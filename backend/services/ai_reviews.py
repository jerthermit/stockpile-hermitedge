import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from database import (
    INTEGRITY_ERRORS,
    begin_write,
    execute_write,
    fetchall,
    fetchone,
    utc_now,
)
from schemas import (
    AIDiscrepancyReview,
    AIDiscrepancyReviewCreate,
    DeliveryDocumentAnalysis,
    DeliveryDocumentExtraction,
    DeliveryDocumentLineMatch,
    DeliveryDocumentProduct,
)
from services.ai import (
    AICompletion,
    AIServiceError,
    DELIVERY_PROMPT_VERSION,
    DISCREPANCY_PROMPT_VERSION,
    MAX_EVIDENCE_BYTES,
    MAX_EVIDENCE_RECORDS,
    analyze_delivery_document,
    prepare_document_image,
    review_discrepancies,
    together_text_model,
    together_vision_model,
)
from services.inventory import (
    list_inventory,
    list_order_comparisons,
)


logger = logging.getLogger("stockpile.services.ai_reviews")

AUTHORITY = "Stockpile application database"
PROCESSING_STALE_SECONDS = 180
DETERMINISTIC_PROVIDER = "stockpile"
DETERMINISTIC_MODEL = "rules-v1"
CACHE_PROVIDER = "stockpile-cache"
MAX_ORDER_BATCHES_PER_REVIEW = 100


@dataclass
class AIReviewError(Exception):
    status_code: int
    code: str
    detail: str
    retryable: bool = False


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_loads(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        logger.error("Stored AI review JSON could not be decoded.")
        return fallback


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _normalize_idempotency_key(value: str) -> str:
    key = value.strip()
    if len(key) < 8 or len(key) > 128:
        raise AIReviewError(
            422,
            "invalid_idempotency_key",
            "The request key must contain 8 to 128 characters.",
        )
    return key


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(
                value.strip().replace("Z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _as_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def _clip(value: Any, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text[:limit]


def _load_review(
    db: Any,
    tenant: str,
    *,
    review_id: str | None = None,
    idempotency_key: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    if (review_id is None) == (idempotency_key is None):
        raise ValueError("Load an AI review by exactly one identifier.")

    if review_id is not None:
        sqlite_where = "tenant_id = ? AND id = ?"
        postgres_where = "tenant_id = %s AND id = %s"
        value = review_id
    else:
        sqlite_where = "tenant_id = ? AND idempotency_key = ?"
        postgres_where = "tenant_id = %s AND idempotency_key = %s"
        value = idempotency_key

    return fetchone(
        db,
        f"SELECT * FROM ai_reviews WHERE {sqlite_where}",
        (tenant, value),
        (
            f"SELECT * FROM ai_reviews WHERE {postgres_where}"
            + (" FOR UPDATE" if for_update else "")
        ),
    )


def ai_review_public(record: dict[str, Any]) -> dict[str, Any]:
    evidence = _json_loads(record.get("evidence_json"), {})
    result = _json_loads(record.get("result_json"), None)
    source = None
    if record["review_type"] == "delivery_document":
        source = {
            "filename": record.get("source_filename"),
            "media_type": record.get("source_media_type"),
            "size_bytes": record.get("source_size_bytes"),
            "sha256": record.get("source_sha256"),
        }

    records = evidence.get("records", []) if isinstance(evidence, dict) else []
    if not isinstance(records, list):
        records = []

    error = None
    if record["status"] == "failed":
        error = {
            "code": record.get("error_code"),
            "message": record.get("error_message"),
        }

    return {
        "id": record["id"],
        "review_type": record["review_type"],
        "status": record["status"],
        "provider": record["provider"],
        "model": record["model"],
        "prompt_version": record["prompt_version"],
        "source": source,
        "window_hours": record.get("window_hours"),
        "evidence_count": len(records),
        "omitted_evidence_count": (
            int(evidence.get("omitted_count", 0))
            if isinstance(evidence, dict)
            else 0
        ),
        "result": result,
        "error": error,
        "attempts": int(record.get("attempts") or 0),
        "usage": {
            "prompt_tokens": record.get("prompt_tokens"),
            "completion_tokens": record.get("completion_tokens"),
            "total_tokens": record.get("total_tokens"),
            "latency_ms": record.get("latency_ms"),
            "provider_request_id": record.get("provider_request_id"),
        },
        "requested_by_user_id": record["requested_by_user_id"],
        "requested_by_name": record["requested_by_name"],
        "created_at": _iso(record["created_at"]),
        "last_attempt_at": _iso(record.get("last_attempt_at")),
        "completed_at": _iso(record.get("completed_at")),
    }


def _review_response(
    record: dict[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "authority": AUTHORITY,
        "data": ai_review_public(record),
        "replayed": replayed,
    }


def _processing_is_stale(record: dict[str, Any], now: datetime) -> bool:
    attempted_at = _as_datetime(record.get("last_attempt_at"))
    if attempted_at is None:
        return True
    return attempted_at <= now - timedelta(seconds=PROCESSING_STALE_SECONDS)


def _claim_review(
    db: Any,
    user: dict[str, Any],
    *,
    idempotency_key: str,
    request_fingerprint: str,
    review_type: str,
    provider: str,
    model: str,
    prompt_version: str,
    evidence: dict[str, Any],
    source_filename: str | None = None,
    source_media_type: str | None = None,
    source_size_bytes: int | None = None,
    source_sha256: str | None = None,
    window_hours: int | None = None,
) -> tuple[dict[str, Any], bool, str | None]:
    tenant = user["tenant_id"]
    key = _normalize_idempotency_key(idempotency_key)

    for collision_attempt in range(2):
        attempt_at = utc_now()
        now = _as_datetime(attempt_at) or datetime.now(timezone.utc)
        try:
            begin_write(db)
            existing = _load_review(
                db,
                tenant,
                idempotency_key=key,
                for_update=True,
            )

            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise AIReviewError(
                        409,
                        "idempotency_key_conflict",
                        "This request key was already used for different review data.",
                    )

                if existing["review_type"] != review_type:
                    raise AIReviewError(
                        409,
                        "idempotency_key_conflict",
                        "This request key was already used for another review type.",
                    )

                if existing["status"] == "completed":
                    db.commit()
                    return existing, True, None

                if (
                    existing["status"] == "processing"
                    and not _processing_is_stale(existing, now)
                ):
                    raise AIReviewError(
                        409,
                        "review_in_progress",
                        "This review is already running.",
                        retryable=True,
                    )

                execute_write(
                    db,
                    """
                    UPDATE ai_reviews
                    SET status = 'processing', provider = ?, model = ?,
                        prompt_version = ?, result_json = NULL,
                        error_code = NULL,
                        error_message = NULL, attempts = attempts + 1,
                        prompt_tokens = NULL, completion_tokens = NULL,
                        total_tokens = NULL, latency_ms = NULL,
                        provider_request_id = NULL,
                        last_attempt_at = ?, completed_at = NULL
                    WHERE tenant_id = ? AND id = ?
                    """,
                    (
                        provider,
                        model,
                        prompt_version,
                        attempt_at,
                        tenant,
                        existing["id"],
                    ),
                    """
                    UPDATE ai_reviews
                    SET status = 'processing', provider = %s, model = %s,
                        prompt_version = %s, result_json = NULL,
                        error_code = NULL,
                        error_message = NULL, attempts = attempts + 1,
                        prompt_tokens = NULL, completion_tokens = NULL,
                        total_tokens = NULL, latency_ms = NULL,
                        provider_request_id = NULL,
                        last_attempt_at = %s, completed_at = NULL
                    WHERE tenant_id = %s AND id = %s
                    """,
                )
                review_id = existing["id"]
            else:
                review_id = str(uuid.uuid4())
                execute_write(
                    db,
                    """
                    INSERT INTO ai_reviews
                        (id, tenant_id, idempotency_key,
                         request_fingerprint, review_type, status,
                         provider, model, prompt_version,
                         source_filename, source_media_type,
                         source_size_bytes, source_sha256, window_hours,
                         evidence_json, attempts, requested_by_user_id,
                         requested_by_name, created_at, last_attempt_at)
                    VALUES (?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        tenant,
                        key,
                        request_fingerprint,
                        review_type,
                        provider,
                        model,
                        prompt_version,
                        source_filename,
                        source_media_type,
                        source_size_bytes,
                        source_sha256,
                        window_hours,
                        _json_dumps(evidence),
                        user["id"],
                        user["display_name"],
                        attempt_at,
                        attempt_at,
                    ),
                    """
                    INSERT INTO ai_reviews
                        (id, tenant_id, idempotency_key,
                         request_fingerprint, review_type, status,
                         provider, model, prompt_version,
                         source_filename, source_media_type,
                         source_size_bytes, source_sha256, window_hours,
                         evidence_json, attempts, requested_by_user_id,
                         requested_by_name, created_at, last_attempt_at)
                    VALUES (%s, %s, %s, %s, %s, 'processing', %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s)
                    """,
                )

            db.commit()
            claimed = _load_review(db, tenant, review_id=review_id)
            db.commit()
            if claimed is None:
                raise RuntimeError("Claimed AI review could not be reloaded.")
            return claimed, False, attempt_at
        except INTEGRITY_ERRORS:
            db.rollback()
            if collision_attempt == 0:
                continue
            raise AIReviewError(
                409,
                "review_request_conflict",
                "The review request could not be claimed. Retry with a new request key.",
                retryable=True,
            )
        except AIReviewError:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise

    raise RuntimeError("AI review claim did not finish.")


def _complete_review(
    db: Any,
    tenant: str,
    review_id: str,
    attempt_at: str,
    *,
    result: dict[str, Any],
    evidence: dict[str, Any],
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    latency_ms: int | None,
    provider_request_id: str | None,
) -> dict[str, Any]:
    completed_at = utc_now()
    try:
        begin_write(db)
        updated = execute_write(
            db,
            """
            UPDATE ai_reviews
            SET status = 'completed', evidence_json = ?, result_json = ?,
                provider = ?, model = ?, prompt_tokens = ?,
                completion_tokens = ?, total_tokens = ?, latency_ms = ?,
                provider_request_id = ?, completed_at = ?
            WHERE tenant_id = ? AND id = ? AND status = 'processing'
              AND last_attempt_at = ?
            """,
            (
                _json_dumps(evidence),
                _json_dumps(result),
                provider,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                latency_ms,
                provider_request_id,
                completed_at,
                tenant,
                review_id,
                attempt_at,
            ),
            """
            UPDATE ai_reviews
            SET status = 'completed', evidence_json = %s, result_json = %s,
                provider = %s, model = %s, prompt_tokens = %s,
                completion_tokens = %s, total_tokens = %s, latency_ms = %s,
                provider_request_id = %s, completed_at = %s
            WHERE tenant_id = %s AND id = %s AND status = 'processing'
              AND last_attempt_at = %s
            """,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    if updated != 1:
        raise AIReviewError(
            409,
            "review_attempt_superseded",
            "A newer attempt replaced this review. Reload the latest result.",
            retryable=True,
        )

    record = _load_review(db, tenant, review_id=review_id)
    if record is None:
        raise RuntimeError("Completed AI review could not be reloaded.")
    return record


def _fail_review(
    db: Any,
    tenant: str,
    review_id: str,
    attempt_at: str,
    error: AIReviewError,
) -> None:
    try:
        begin_write(db)
        execute_write(
            db,
            """
            UPDATE ai_reviews
            SET status = 'failed', result_json = NULL,
                error_code = ?, error_message = ?, completed_at = ?
            WHERE tenant_id = ? AND id = ? AND status = 'processing'
              AND last_attempt_at = ?
            """,
            (
                error.code[:80],
                error.detail[:1_000],
                utc_now(),
                tenant,
                review_id,
                attempt_at,
            ),
            """
            UPDATE ai_reviews
            SET status = 'failed', result_json = NULL,
                error_code = %s, error_message = %s, completed_at = %s
            WHERE tenant_id = %s AND id = %s AND status = 'processing'
              AND last_attempt_at = %s
            """,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("AI review failure state could not be recorded.")


def _provider_error(error: AIServiceError) -> AIReviewError:
    return AIReviewError(
        error.status_code,
        error.code,
        error.detail,
        error.retryable,
    )


def _completion_values(
    completion: AICompletion[Any],
) -> dict[str, Any]:
    return {
        "provider": completion.provider,
        "model": completion.model,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "total_tokens": completion.total_tokens,
        "latency_ms": completion.latency_ms,
        "provider_request_id": completion.provider_request_id,
    }


def _normalized_unit(value: str) -> str:
    unit = " ".join(value.strip().lower().replace(".", "").split())
    aliases = {
        "pc": "piece",
        "pcs": "piece",
        "pieces": "piece",
        "rolls": "roll",
        "boxes": "box",
        "packs": "pack",
        "sets": "set",
        "meters": "meter",
        "metres": "meter",
        "m": "meter",
        "kgs": "kilogram",
        "kg": "kilogram",
    }
    return aliases.get(unit, unit)


def _units_compatible(extracted: str | None, catalog: str | None) -> bool:
    if extracted is None or catalog is None:
        return True
    catalog_unit = _normalized_unit(catalog)
    if catalog_unit in {"unit", "item"}:
        return True
    return _normalized_unit(extracted) == catalog_unit


def _document_product(item: dict[str, Any]) -> DeliveryDocumentProduct:
    return DeliveryDocumentProduct(
        sku=item["sku"],
        barcode=item["barcode"],
        product_name=item["product_name"],
        unit=item.get("unit") or "unit",
    )


def _delivery_analysis(
    extraction: DeliveryDocumentExtraction,
    catalog: list[dict[str, Any]],
) -> DeliveryDocumentAnalysis:
    by_sku = {item["sku"]: item for item in catalog}
    by_barcode = {item["barcode"]: item for item in catalog}
    candidates: dict[int, dict[str, Any] | None] = {}
    statuses: dict[int, str | None] = {}

    for line in extraction.lines:
        sku_match = by_sku.get(line.sku) if line.sku is not None else None
        barcode_match = (
            by_barcode.get(line.barcode)
            if line.barcode is not None
            else None
        )

        if line.sku is not None and line.barcode is not None:
            if sku_match is None and barcode_match is None:
                candidates[line.source_line] = None
                statuses[line.source_line] = "unknown_product"
            elif (
                sku_match is None
                or barcode_match is None
                or sku_match["sku"] != barcode_match["sku"]
            ):
                candidates[line.source_line] = None
                statuses[line.source_line] = "identifier_conflict"
            else:
                candidates[line.source_line] = sku_match
                statuses[line.source_line] = None
        elif line.sku is not None:
            candidates[line.source_line] = sku_match
            statuses[line.source_line] = (
                None if sku_match is not None else "unknown_product"
            )
        elif line.barcode is not None:
            candidates[line.source_line] = barcode_match
            statuses[line.source_line] = (
                None if barcode_match is not None else "unknown_product"
            )
        else:
            candidates[line.source_line] = None
            statuses[line.source_line] = "unknown_product"

        candidate = candidates[line.source_line]
        if candidate is not None and not _units_compatible(
            line.unit,
            candidate.get("unit"),
        ):
            candidates[line.source_line] = None
            statuses[line.source_line] = "identifier_conflict"

    candidate_counts: dict[str, int] = {}
    for candidate in candidates.values():
        if candidate is not None:
            sku = candidate["sku"]
            candidate_counts[sku] = candidate_counts.get(sku, 0) + 1

    matches: list[DeliveryDocumentLineMatch] = []
    for line in extraction.lines:
        candidate = candidates[line.source_line]
        status = statuses[line.source_line]
        product = None

        if candidate is not None:
            product = _document_product(candidate)
            if candidate_counts[candidate["sku"]] > 1:
                status = "duplicate_product"
            elif not bool(candidate.get("active", True)):
                status = "inactive_product"
            elif line.quantity is None:
                status = "invalid_quantity"
            else:
                status = "matched"

        matches.append(
            DeliveryDocumentLineMatch(
                source_line=line.source_line,
                status=status,
                product=product,
            )
        )

    receipt_ready = (
        extraction.read_status == "readable"
        and extraction.document_type in {"delivery_receipt", "invoice"}
        and bool(matches)
        and all(match.status == "matched" for match in matches)
    )
    return DeliveryDocumentAnalysis(
        extraction=extraction,
        matches=matches,
        receipt_ready=receipt_ready,
    )


def _delivery_evidence(
    evidence: dict[str, Any],
    analysis: DeliveryDocumentAnalysis,
) -> dict[str, Any]:
    identifiers_checked = [
        {
            "source_line": line.source_line,
            "sku": line.sku,
            "barcode": line.barcode,
        }
        for line in analysis.extraction.lines
    ]
    catalog_matches = []
    seen_skus: set[str] = set()
    for match in analysis.matches:
        if match.product is None or match.product.sku in seen_skus:
            continue
        seen_skus.add(match.product.sku)
        catalog_matches.append(match.product.model_dump(mode="json"))
    return {
        **evidence,
        "identifiers_checked": identifiers_checked,
        "catalog_matches": catalog_matches,
    }


def _matching_delivery_extraction(
    db: Any,
    tenant: str,
    *,
    source_sha256: str,
    model: str,
) -> tuple[dict[str, Any], DeliveryDocumentExtraction] | None:
    for record in _list_review_rows(
        db,
        tenant,
        review_type="delivery_document",
        limit=20,
    ):
        if (
            record.get("status") != "completed"
            or record.get("provider") not in {"together", CACHE_PROVIDER}
            or record.get("model") != model
            or record.get("prompt_version") != DELIVERY_PROMPT_VERSION
            or record.get("source_sha256") != source_sha256
        ):
            continue
        result = _json_loads(record.get("result_json"), None)
        if not isinstance(result, dict):
            continue
        try:
            analysis = DeliveryDocumentAnalysis.model_validate(result)
            return record, analysis.extraction
        except (ValidationError, ValueError):
            continue
    return None


def create_delivery_document_review(
    db: Any,
    user: dict[str, Any],
    *,
    filename: str | None,
    content_type: str | None,
    content: bytes,
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        image = prepare_document_image(filename, content_type, content)
    except AIServiceError as error:
        raise _provider_error(error)

    evidence: dict[str, Any] = {
        "document": {
            "filename": image.filename,
            "media_type": image.source_media_type,
            "size_bytes": image.source_size_bytes,
            "sha256": image.source_sha256,
            "width": image.width,
            "height": image.height,
        },
        "identifiers_checked": [],
        "catalog_matches": [],
    }
    request_fingerprint = _fingerprint(
        {
            "review_type": "delivery_document",
            "source_sha256": image.source_sha256,
        }
    )
    model = together_vision_model()
    claimed, replayed, attempt_at = _claim_review(
        db,
        user,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        review_type="delivery_document",
        provider="together",
        model=model,
        prompt_version=DELIVERY_PROMPT_VERSION,
        evidence=evidence,
        source_filename=image.filename,
        source_media_type=image.source_media_type,
        source_size_bytes=image.source_size_bytes,
        source_sha256=image.source_sha256,
    )
    if replayed:
        return _review_response(claimed, replayed=True)
    if attempt_at is None:
        raise RuntimeError("Delivery review attempt was not claimed.")

    tenant = user["tenant_id"]
    catalog = list_inventory(db, tenant)
    cached = _matching_delivery_extraction(
        db,
        tenant,
        source_sha256=image.source_sha256,
        model=model,
    )
    if cached is not None:
        source, extraction = cached
        analysis = _delivery_analysis(extraction, catalog)
        evidence = _delivery_evidence(evidence, analysis)
        completed = _complete_review(
            db,
            tenant,
            claimed["id"],
            attempt_at,
            result=analysis.model_dump(mode="json"),
            evidence=evidence,
            provider=CACHE_PROVIDER,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=0,
            provider_request_id=f"reused:{source['id']}",
        )
        return _review_response(completed, replayed=True)

    try:
        completion = analyze_delivery_document(image)
        analysis = _delivery_analysis(completion.output, catalog)
        evidence = _delivery_evidence(evidence, analysis)

        completed = _complete_review(
            db,
            tenant,
            claimed["id"],
            attempt_at,
            result=analysis.model_dump(mode="json"),
            evidence=evidence,
            **_completion_values(completion),
        )
        return _review_response(completed, replayed=False)
    except AIServiceError as error:
        review_error = _provider_error(error)
        _fail_review(db, tenant, claimed["id"], attempt_at, review_error)
        raise review_error
    except AIReviewError:
        raise
    except Exception:
        logger.exception("Delivery document review failed.")
        review_error = AIReviewError(
            500,
            "delivery_review_failed",
            "The document review could not be completed.",
            retryable=True,
        )
        _fail_review(db, tenant, claimed["id"], attempt_at, review_error)
        raise review_error


def _build_discrepancy_evidence(
    db: Any,
    tenant: str,
    *,
    as_of: datetime,
    window_hours: int,
) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    source_total = 0
    as_of_value = as_of.isoformat()
    per_source_limit = MAX_EVIDENCE_RECORDS + 1

    investigations = fetchall(
        db,
        """
        SELECT id, kind, sku, barcode, product_name, quantity,
               expected_location_path, observed_location_path,
               opened_employee_name, reference, note, created_at,
               COUNT(*) OVER () AS source_total
        FROM inventory_investigations
        WHERE tenant_id = ? AND status = 'open'
          AND created_at <= ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (tenant, as_of_value, per_source_limit),
        """
        SELECT id, kind, sku, barcode, product_name, quantity,
               expected_location_path, observed_location_path,
               opened_employee_name, reference, note, created_at,
               COUNT(*) OVER () AS source_total
        FROM inventory_investigations
        WHERE tenant_id = %s AND status = 'open'
          AND created_at <= %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
    )
    if investigations:
        source_total += int(investigations[0]["source_total"])
    for record in investigations:
        records.append(
            {
                "evidence_ref": f"investigation:{record['id']}",
                "kind": f"{record['kind']}_stock",
                "occurred_at": _iso(record.get("created_at")),
                "product": {
                    "sku": record["sku"],
                    "barcode": record["barcode"],
                    "name": record["product_name"],
                },
                "quantity": int(record["quantity"]),
                "expected_location": _clip(
                    record.get("expected_location_path")
                ),
                "observed_location": _clip(
                    record.get("observed_location_path")
                ),
                "opened_by": _clip(record.get("opened_employee_name"), 120),
                "reference": _clip(record.get("reference"), 120),
                "note": _clip(record.get("note")),
            }
        )

    transfer_lines = fetchall(
        db,
        """
        SELECT handoff.id AS transfer_id, line.sku, line.barcode,
               line.product_name, line.quantity_sent,
               line.quantity_received, line.discrepancy_note,
               handoff.source_location_path,
               handoff.destination_location_path,
               handoff.initiated_by_name, handoff.received_by_name,
               COALESCE(handoff.received_at, handoff.created_at) AS occurred_at,
               COUNT(*) OVER () AS source_total
        FROM inventory_transfer_lines AS line
        JOIN inventory_transfers AS handoff
          ON handoff.tenant_id = line.tenant_id
         AND handoff.id = line.transfer_id
        WHERE handoff.tenant_id = ?
          AND handoff.status = 'received_with_discrepancy'
          AND COALESCE(handoff.received_at, handoff.created_at) <= ?
          AND (
              line.quantity_received IS NULL
              OR line.quantity_received <> line.quantity_sent
              OR line.discrepancy_note IS NOT NULL
          )
        ORDER BY occurred_at DESC, handoff.id DESC, line.sku
        LIMIT ?
        """,
        (tenant, as_of_value, per_source_limit),
        """
        SELECT handoff.id AS transfer_id, line.sku, line.barcode,
               line.product_name, line.quantity_sent,
               line.quantity_received, line.discrepancy_note,
               handoff.source_location_path,
               handoff.destination_location_path,
               handoff.initiated_by_name, handoff.received_by_name,
               COALESCE(handoff.received_at, handoff.created_at) AS occurred_at,
               COUNT(*) OVER () AS source_total
        FROM inventory_transfer_lines AS line
        JOIN inventory_transfers AS handoff
          ON handoff.tenant_id = line.tenant_id
         AND handoff.id = line.transfer_id
        WHERE handoff.tenant_id = %s
          AND handoff.status = 'received_with_discrepancy'
          AND COALESCE(handoff.received_at, handoff.created_at) <= %s
          AND (
              line.quantity_received IS NULL
              OR line.quantity_received <> line.quantity_sent
              OR line.discrepancy_note IS NOT NULL
          )
        ORDER BY occurred_at DESC, handoff.id DESC, line.sku
        LIMIT %s
        """,
    )
    if transfer_lines:
        source_total += int(transfer_lines[0]["source_total"])
    for line in transfer_lines:
        received = line.get("quantity_received")
        records.append(
            {
                "evidence_ref": (
                    f"transfer:{line['transfer_id']}:{line['sku']}"
                ),
                "kind": "handoff_difference",
                "occurred_at": _iso(line.get("occurred_at")),
                "product": {
                    "sku": line["sku"],
                    "barcode": line["barcode"],
                    "name": line["product_name"],
                },
                "quantity_sent": int(line["quantity_sent"]),
                "quantity_received": (
                    int(received) if received is not None else None
                ),
                "from": _clip(line.get("source_location_path")),
                "to": _clip(line.get("destination_location_path")),
                "sent_by": _clip(line.get("initiated_by_name"), 120),
                "received_by": _clip(line.get("received_by_name"), 120),
                "note": _clip(line.get("discrepancy_note")),
            }
        )

    damage_rows = fetchall(
        db,
        """
        SELECT id, sku, barcode, product_name, remaining_quantity,
               location_path, category, reason, reported_by_name,
               reference, created_at, COUNT(*) OVER () AS source_total
        FROM inventory_damage
        WHERE tenant_id = ? AND status = 'open' AND remaining_quantity > 0
          AND created_at <= ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (tenant, as_of_value, per_source_limit),
        """
        SELECT id, sku, barcode, product_name, remaining_quantity,
               location_path, category, reason, reported_by_name,
               reference, created_at, COUNT(*) OVER () AS source_total
        FROM inventory_damage
        WHERE tenant_id = %s AND status = 'open' AND remaining_quantity > 0
          AND created_at <= %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
    )
    if damage_rows:
        source_total += int(damage_rows[0]["source_total"])
    for record in damage_rows:
        records.append(
            {
                "evidence_ref": f"damage:{record['id']}",
                "kind": "damaged_stock",
                "occurred_at": _iso(record.get("created_at")),
                "product": {
                    "sku": record["sku"],
                    "barcode": record["barcode"],
                    "name": record["product_name"],
                },
                "quantity": int(record["remaining_quantity"]),
                "location": _clip(record.get("location_path")),
                "category": record.get("category"),
                "reason": _clip(record.get("reason")),
                "reported_by": _clip(
                    record.get("reported_by_name"), 120
                ),
                "reference": _clip(record.get("reference"), 120),
            }
        )

    count_lines = fetchall(
        db,
        """
        SELECT stock_count.id AS count_id, stock_count.location_path,
               stock_count.counter_name, stock_count.note,
               stock_count.created_at, line.sku, line.barcode,
               line.product_name, line.expected_quantity,
               line.counted_quantity, line.difference,
               COUNT(*) OVER () AS source_total
        FROM inventory_stock_count_lines AS line
        JOIN inventory_stock_counts AS stock_count
          ON stock_count.tenant_id = line.tenant_id
         AND stock_count.id = line.count_id
        WHERE stock_count.tenant_id = ? AND stock_count.status = 'pending'
          AND line.difference <> 0
          AND stock_count.created_at <= ?
        ORDER BY stock_count.created_at DESC, stock_count.id DESC, line.sku
        LIMIT ?
        """,
        (tenant, as_of_value, per_source_limit),
        """
        SELECT stock_count.id AS count_id, stock_count.location_path,
               stock_count.counter_name, stock_count.note,
               stock_count.created_at, line.sku, line.barcode,
               line.product_name, line.expected_quantity,
               line.counted_quantity, line.difference,
               COUNT(*) OVER () AS source_total
        FROM inventory_stock_count_lines AS line
        JOIN inventory_stock_counts AS stock_count
          ON stock_count.tenant_id = line.tenant_id
         AND stock_count.id = line.count_id
        WHERE stock_count.tenant_id = %s AND stock_count.status = 'pending'
          AND line.difference <> 0
          AND stock_count.created_at <= %s
        ORDER BY stock_count.created_at DESC, stock_count.id DESC, line.sku
        LIMIT %s
        """,
    )
    if count_lines:
        source_total += int(count_lines[0]["source_total"])
    for line in count_lines:
        records.append(
            {
                "evidence_ref": f"count:{line['count_id']}:{line['sku']}",
                "kind": "physical_count_difference",
                "occurred_at": _iso(line.get("created_at")),
                "product": {
                    "sku": line["sku"],
                    "barcode": line["barcode"],
                    "name": line["product_name"],
                },
                "expected_quantity": int(line["expected_quantity"]),
                "counted_quantity": int(line["counted_quantity"]),
                "difference": int(line["difference"]),
                "location": _clip(line.get("location_path")),
                "counted_by": _clip(line.get("counter_name"), 120),
                "note": _clip(line.get("note")),
            }
        )

    for batch in list_order_comparisons(
        db,
        tenant,
        limit=MAX_ORDER_BATCHES_PER_REVIEW,
    ):
        batch_created_at = _as_datetime(batch.get("created_at"))
        if batch_created_at is None or batch_created_at > as_of:
            continue
        for line in batch.get("lines", []):
            if not line.get("needs_attention"):
                continue
            source_total += 1
            records.append(
                {
                    "evidence_ref": f"order:{batch['id']}:{line['id']}",
                    "kind": "selling_order_difference",
                    "occurred_at": _iso(batch.get("created_at")),
                    "sales_channel": _clip(
                        batch.get("sales_channel"), 80
                    ),
                    "order_reference": _clip(
                        line.get("order_reference"), 120
                    ),
                    "product": {
                        "sku": line.get("sku") or line.get("submitted_sku"),
                        "barcode": (
                            line.get("barcode")
                            or line.get("submitted_barcode")
                        ),
                        "name": line.get("product_name"),
                    },
                    "ordered_quantity": int(line["ordered_quantity"]),
                    "recorded_quantity": int(
                        line.get("effective_stockpile_quantity") or 0
                    ),
                    "status": line.get("current_comparison_status"),
                    "evidence_issue": _clip(line.get("evidence_issue")),
                    "product_mapping_issue": _clip(
                        line.get("product_mapping_issue")
                    ),
                    "stale": bool(line.get("stale")),
                    "resolution": line.get("resolution"),
                }
            )

    records.sort(
        key=lambda record: (
            record.get("occurred_at") or "",
            record["evidence_ref"],
        ),
        reverse=True,
    )
    selected = records[:MAX_EVIDENCE_RECORDS]
    while (
        selected
        and len(_json_dumps(selected).encode("utf-8")) > MAX_EVIDENCE_BYTES
    ):
        selected.pop()
    return selected, max(0, source_total - len(selected))


def _matching_discrepancy_review(
    db: Any,
    tenant: str,
    *,
    records: list[dict[str, Any]],
    omitted_count: int,
    window_hours: int,
    model: str,
) -> tuple[dict[str, Any], AIDiscrepancyReview] | None:
    if omitted_count:
        return None

    target = _fingerprint(
        {
            "records": records,
            "omitted_count": omitted_count,
            "window_hours": window_hours,
        }
    )
    for record in _list_review_rows(
        db,
        tenant,
        review_type="discrepancy_review",
        limit=20,
    ):
        if (
            record.get("status") != "completed"
            or record.get("provider") not in {"together", CACHE_PROVIDER}
            or record.get("model") != model
            or record.get("prompt_version") != DISCREPANCY_PROMPT_VERSION
        ):
            continue

        evidence = _json_loads(record.get("evidence_json"), {})
        result = _json_loads(record.get("result_json"), None)
        if not isinstance(evidence, dict) or not isinstance(result, dict):
            continue
        candidate = _fingerprint(
            {
                "records": evidence.get("records"),
                "omitted_count": evidence.get("omitted_count", 0),
                "window_hours": evidence.get("window_hours"),
            }
        )
        if candidate != target:
            continue
        try:
            return record, AIDiscrepancyReview.model_validate(result)
        except (ValidationError, ValueError):
            continue

    return None


def create_discrepancy_review(
    db: Any,
    user: dict[str, Any],
    payload: AIDiscrepancyReviewCreate,
) -> dict[str, Any]:
    as_of = datetime.now(timezone.utc)
    records, omitted_count = _build_discrepancy_evidence(
        db,
        user["tenant_id"],
        as_of=as_of,
        window_hours=payload.window_hours,
    )
    evidence = {
        "window_hours": payload.window_hours,
        "includes_open_carryover": True,
        "window_started_at": (
            as_of - timedelta(hours=payload.window_hours)
        ).isoformat(),
        "reviewed_through": as_of.isoformat(),
        "records": records,
        "omitted_count": omitted_count,
    }
    model = together_text_model()
    request_fingerprint = _fingerprint(
        {
            "review_type": "discrepancy_review",
            "window_hours": payload.window_hours,
        }
    )
    claimed, replayed, attempt_at = _claim_review(
        db,
        user,
        idempotency_key=payload.idempotency_key,
        request_fingerprint=request_fingerprint,
        review_type="discrepancy_review",
        provider="together",
        model=model,
        prompt_version=DISCREPANCY_PROMPT_VERSION,
        evidence=evidence,
        window_hours=payload.window_hours,
    )
    if replayed:
        return _review_response(claimed, replayed=True)
    if attempt_at is None:
        raise RuntimeError("Discrepancy review attempt was not claimed.")

    tenant = user["tenant_id"]
    stored_evidence = _json_loads(claimed.get("evidence_json"), {})
    if not isinstance(stored_evidence, dict):
        review_error = AIReviewError(
            500,
            "invalid_review_evidence",
            "The saved discrepancy evidence could not be read.",
        )
        _fail_review(db, tenant, claimed["id"], attempt_at, review_error)
        raise review_error
    stored_records = stored_evidence.get("records")
    if not isinstance(stored_records, list):
        review_error = AIReviewError(
            500,
            "invalid_review_evidence",
            "The saved discrepancy evidence could not be read.",
        )
        _fail_review(db, tenant, claimed["id"], attempt_at, review_error)
        raise review_error
    evidence = stored_evidence
    records = stored_records
    stored_omitted = evidence.get("omitted_count")
    omitted_count = (
        stored_omitted
        if isinstance(stored_omitted, int) and stored_omitted >= 0
        else 0
    )

    if not records:
        result = AIDiscrepancyReview(
            review_status="no_issues",
            summary=(
                "No unresolved discrepancies need attention."
            ),
            findings=[],
            questions_for_staff=[],
            limitations=[],
            confidence="high",
        )
        completed = _complete_review(
            db,
            tenant,
            claimed["id"],
            attempt_at,
            result=result.model_dump(mode="json"),
            evidence=evidence,
            provider=DETERMINISTIC_PROVIDER,
            model=DETERMINISTIC_MODEL,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=0,
            provider_request_id=None,
        )
        return _review_response(completed, replayed=False)

    cached = _matching_discrepancy_review(
        db,
        tenant,
        records=records,
        omitted_count=omitted_count,
        window_hours=payload.window_hours,
        model=model,
    )
    if cached is not None:
        source, result = cached
        completed = _complete_review(
            db,
            tenant,
            claimed["id"],
            attempt_at,
            result=result.model_dump(mode="json"),
            evidence=evidence,
            provider=CACHE_PROVIDER,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=0,
            provider_request_id=f"reused:{source['id']}",
        )
        return _review_response(completed, replayed=True)

    try:
        completion = review_discrepancies(records)
        output = completion.output
        if omitted_count:
            output_data = output.model_dump(mode="json")
            notice = (
                f"The {MAX_EVIDENCE_RECORDS} most recent records were reviewed; "
                f"{omitted_count} older record(s) were omitted."
            )
            output_data["limitations"] = [notice]
            output = AIDiscrepancyReview.model_validate(output_data)

        completed = _complete_review(
            db,
            tenant,
            claimed["id"],
            attempt_at,
            result=output.model_dump(mode="json"),
            evidence=evidence,
            **_completion_values(completion),
        )
        return _review_response(completed, replayed=False)
    except AIServiceError as error:
        review_error = _provider_error(error)
        _fail_review(db, tenant, claimed["id"], attempt_at, review_error)
        raise review_error
    except AIReviewError:
        raise
    except Exception:
        logger.exception("Discrepancy review failed.")
        review_error = AIReviewError(
            500,
            "discrepancy_review_failed",
            "The discrepancy review could not be completed.",
            retryable=True,
        )
        _fail_review(db, tenant, claimed["id"], attempt_at, review_error)
        raise review_error


def list_ai_reviews(
    db: Any,
    tenant: str,
    *,
    review_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if review_type not in {
        None,
        "delivery_document",
        "discrepancy_review",
    }:
        raise AIReviewError(
            422,
            "invalid_review_type",
            "AI review type is invalid.",
        )
    if limit < 1 or limit > 100:
        raise AIReviewError(
            422,
            "invalid_review_limit",
            "AI review limit must be between 1 and 100.",
        )

    if review_type is None:
        rows = _list_review_rows(db, tenant, limit=limit)
    else:
        rows = _list_review_rows(
            db,
            tenant,
            review_type=review_type,
            limit=limit,
        )
    return [ai_review_public(row) for row in rows]


def _list_review_rows(
    db: Any,
    tenant: str,
    *,
    limit: int,
    review_type: str | None = None,
) -> list[dict[str, Any]]:
    from database import fetchall

    if review_type is None:
        return fetchall(
            db,
            """
            SELECT * FROM ai_reviews
            WHERE tenant_id = ?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (tenant, limit),
            """
            SELECT * FROM ai_reviews
            WHERE tenant_id = %s
            ORDER BY created_at DESC, id DESC LIMIT %s
            """,
        )
    return fetchall(
        db,
        """
        SELECT * FROM ai_reviews
        WHERE tenant_id = ? AND review_type = ?
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (tenant, review_type, limit),
        """
        SELECT * FROM ai_reviews
        WHERE tenant_id = %s AND review_type = %s
        ORDER BY created_at DESC, id DESC LIMIT %s
        """,
    )


def get_ai_review(
    db: Any,
    tenant: str,
    review_id: str,
) -> dict[str, Any]:
    record = _load_review(db, tenant, review_id=review_id)
    if record is None:
        raise AIReviewError(
            404,
            "review_not_found",
            "AI review was not found.",
        )
    return ai_review_public(record)
