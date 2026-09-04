import hashlib
import logging
import os
import re
import uuid
import warnings
from functools import partial
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from database import fetchone, get_db
from dependencies import (
    require_admin,
    require_operator,
)
from schemas import (
    AIDiscrepancyReviewCreate,
    DamageCreate,
    DamageResolve,
    ExchangeRetryRequest,
    InvestigationCreate,
    InvestigationEvidenceCreate,
    InvestigationResolve,
    OrderComparisonResolve,
    ProductReceiveCreate,
    ProductUpdate,
    ReceiptCreate,
    ReservationClose,
    ReservationCreate,
    ReversalCreate,
    ReturnCreate,
    StockCountApprove,
    StockCountCancel,
    StockCountCreate,
    TransactionCreate,
    TransferCreate,
    TransferReceive,
)
from services.ai import MAX_DOCUMENT_BYTES
from services.ai_reviews import (
    AIReviewError,
    create_delivery_document_review,
    create_discrepancy_review,
    list_ai_reviews,
)
from services.inventory import (
    DomainError,
    approve_stock_count,
    cancel_stock_count,
    close_reservation,
    create_investigation,
    create_damage,
    create_product_with_stock,
    create_receipt,
    create_reservation,
    create_return,
    create_stock_count,
    create_transaction,
    create_transfer,
    find_by_barcode,
    import_inventory_csv,
    import_selling_orders_csv,
    list_investigations,
    list_inventory,
    list_order_comparisons,
    list_locations,
    list_damage,
    list_receipts,
    list_reservations,
    list_returns,
    list_stock_counts,
    list_transfers,
    receive_transfer,
    add_investigation_evidence,
    replace_product_image_metadata,
    resolve_damage,
    resolve_investigation,
    resolve_order_comparison,
    reverse_transaction,
    transaction_history,
    update_product,
)
from services.sheets import (
    list_exchange_records,
    retry_exchange_records,
)


logger = logging.getLogger(
    "stockpile.api.inventory"
)

router = APIRouter(
    prefix="/api/v1/inventory",
    tags=["Inventory"],
)

AUTHORITY = (
    "Stockpile application database"
)
MAX_IMPORT_BYTES = 2_000_000
MAX_PRODUCT_IMAGE_BYTES = 10_000_000
MAX_PRODUCT_IMAGE_PIXELS = 32_000_000
MAX_PRODUCT_IMAGE_EDGE = 1_600
TransferStatus = Literal[
    "pending",
    "received",
    "received_with_discrepancy",
    "cancelled",
]
DamageStatus = Literal[
    "open",
    "resolved",
]
ReservationStatus = Literal[
    "active",
    "released",
    "fulfilled",
]
StockCountStatus = Literal[
    "pending",
    "applied",
    "cancelled",
]
InvestigationStatus = Literal[
    "open",
    "resolved",
]
InvestigationKind = Literal[
    "missing",
    "misplaced",
    "unexpected",
]
AIReviewType = Literal[
    "delivery_document",
    "discrepancy_review",
]


def data_response(data: Any) -> dict[str, Any]:
    return {
        "authority": AUTHORITY,
        "data": data,
    }


def raise_domain_error(
    error: DomainError,
) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": error.detail,
        },
    )


def raise_ai_review_error(
    error: AIReviewError,
) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": error.detail,
            "retryable": error.retryable,
        },
    )


def raise_ai_review_failure(
    log_message: str,
    client_message: str,
) -> None:
    logger.exception(log_message)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "code": "ai_review_failed",
            "message": client_message,
            "retryable": True,
        },
    )


def raise_write_failure(
    log_message: str,
    client_message: str,
) -> None:
    logger.exception(log_message)

    raise HTTPException(
        status_code=(
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ),
        detail={
            "code": "database_write_failed",
            "message": client_message,
        },
    )


def import_error(
    code: str,
    message: str,
    status_code: int = (
        status.HTTP_422_UNPROCESSABLE_ENTITY
    ),
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
        },
    )


def image_error(
    code: str,
    message: str,
    status_code: int = status.HTTP_422_UNPROCESSABLE_ENTITY,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
        },
    )


def _product_image_root() -> Path:
    configured = os.getenv("STOCKPILE_PRODUCT_IMAGE_DIR", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.cwd() / ".stockpile-product-images").resolve()
    )


def _tenant_image_directory(tenant: str) -> tuple[str, Path]:
    tenant_key = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:24]
    return tenant_key, _product_image_root() / tenant_key


def _product_image_path(storage_key: str) -> Path:
    root = _product_image_root()
    candidate = (root / storage_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise RuntimeError("Product image path escaped the storage directory.") from error
    return candidate


def _product_image_url(sku: str, image_name: str) -> str:
    return (
        "/api/v1/inventory/products/"
        f"{quote(sku, safe='')}/image/{image_name}"
    )


def _prepare_product_image(contents: bytes) -> bytes:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError, features
    except ImportError as error:
        logger.exception("Product image processing is unavailable.")
        raise image_error(
            "image_processing_unavailable",
            "Image uploads are temporarily unavailable.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from error

    if not features.check("webp"):
        logger.error("Pillow was installed without WebP support.")
        raise image_error(
            "image_processing_unavailable",
            "Image uploads are temporarily unavailable.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(contents)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"}:
                    raise image_error(
                        "invalid_product_image",
                        "Upload a JPEG, PNG, or WebP image.",
                    )
                if bool(getattr(source, "is_animated", False)):
                    raise image_error(
                        "invalid_product_image",
                        "Animated product images are not supported.",
                    )
                if source.width * source.height > MAX_PRODUCT_IMAGE_PIXELS:
                    raise image_error(
                        "invalid_product_image",
                        "The product image must be 32 megapixels or smaller.",
                    )
                source.load()
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail(
                    (MAX_PRODUCT_IMAGE_EDGE, MAX_PRODUCT_IMAGE_EDGE),
                    Image.Resampling.LANCZOS,
                )
                has_alpha = oriented.mode in {"RGBA", "LA"} or (
                    "transparency" in oriented.info
                )
                converted = oriented.convert("RGBA" if has_alpha else "RGB")
                output = BytesIO()
                converted.save(
                    output,
                    format="WEBP",
                    quality=84,
                    method=6,
                )
                return output.getvalue()
    except HTTPException:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise image_error(
            "invalid_product_image",
            "The product image must be 32 megapixels or smaller.",
        ) from error
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise image_error(
            "invalid_product_image",
            "The uploaded file is not a usable product image.",
        ) from error


def _store_product_image(tenant: str, contents: bytes) -> tuple[str, str]:
    tenant_key, directory = _tenant_image_directory(tenant)
    directory.mkdir(parents=True, exist_ok=True)
    image_name = f"{uuid.uuid4().hex}.webp"
    storage_key = f"{tenant_key}/{image_name}"
    target = _product_image_path(storage_key)
    temporary = target.with_name(f".{image_name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return storage_key, image_name


def _remove_product_image(storage_key: str | None) -> None:
    if not storage_key:
        return
    try:
        _product_image_path(storage_key).unlink(missing_ok=True)
    except Exception:
        logger.exception("Old product image could not be removed.")


def _product_image_record(
    db: Any,
    tenant: str,
    sku: str,
) -> dict[str, Any] | None:
    return fetchone(
        db,
        """
        SELECT image_url, image_storage_key
        FROM inventory WHERE tenant_id = ? AND sku = ?
        """,
        (tenant, sku),
        """
        SELECT image_url, image_storage_key
        FROM inventory WHERE tenant_id = %s AND sku = %s
        """,
    )


@router.get("")
def get_inventory(
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    return data_response(
        list_inventory(
            db,
            user["tenant_id"],
            include_acquisition_cost=(
                user.get("role") == "admin"
            ),
        )
    )


@router.get("/locations")
def get_locations(
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    return data_response(
        list_locations(
            db,
            user["tenant_id"],
        )
    )


@router.get("/barcodes/{barcode}")
def get_product_by_barcode(
    barcode: str,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    item = find_by_barcode(
        db,
        user["tenant_id"],
        barcode.strip(),
        include_acquisition_cost=(
            user.get("role") == "admin"
        ),
    )

    if item is None:
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
            ),
            detail={
                "code": "unknown_barcode",
                "message": (
                    "No product matches this barcode."
                ),
            },
        )

    return data_response(item)


@router.post(
    "/products",
    status_code=status.HTTP_201_CREATED,
)
def post_product_with_stock(
    payload: ProductReceiveCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        return create_product_with_stock(
            db,
            user,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Product creation failed before completion.",
            (
                "The product and opening stock were "
                "not committed."
            ),
        )


@router.patch("/products/{sku}")
def patch_product(
    sku: str,
    payload: ProductUpdate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        item = update_product(
            db,
            user,
            sku.strip(),
            payload,
        )

        return data_response(item)
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Product update failed before completion.",
            (
                "The product was not changed."
            ),
        )


@router.put("/products/{sku}/image")
async def put_product_image(
    sku: str,
    file: UploadFile = File(...),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        contents = await file.read(
            MAX_PRODUCT_IMAGE_BYTES + 1
        )
    finally:
        await file.close()

    if not contents:
        raise image_error(
            "empty_product_image",
            "Choose a product image to upload.",
        )
    if len(contents) > MAX_PRODUCT_IMAGE_BYTES:
        raise image_error(
            "product_image_too_large",
            "The product image must be 10 MB or smaller.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    prepared = await run_in_threadpool(
        _prepare_product_image,
        contents,
    )
    try:
        storage_key, image_name = await run_in_threadpool(
            _store_product_image,
            user["tenant_id"],
            prepared,
        )
    except Exception:
        logger.exception("Product image could not be stored.")
        raise image_error(
            "product_image_storage_failed",
            "The product image could not be saved.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    normalized_sku = sku.strip().upper()
    image_url = _product_image_url(
        normalized_sku,
        image_name,
    )
    try:
        item, previous_storage_key = (
            replace_product_image_metadata(
                db,
                user,
                normalized_sku,
                image_url=image_url,
                image_storage_key=storage_key,
            )
        )
    except DomainError as error:
        _remove_product_image(storage_key)
        raise_domain_error(error)
    except Exception:
        _remove_product_image(storage_key)
        raise_write_failure(
            "Product image metadata update failed.",
            "The product image was not changed.",
        )

    _remove_product_image(previous_storage_key)
    return data_response(item)


@router.get("/products/{sku}/image/{image_name}")
def get_product_image(
    sku: str,
    image_name: str,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    if re.fullmatch(r"[0-9a-f]{32}\.webp", image_name) is None:
        raise image_error(
            "product_image_not_found",
            "Product image not found.",
            status.HTTP_404_NOT_FOUND,
        )

    normalized_sku = sku.strip().upper()
    tenant_key, _ = _tenant_image_directory(
        user["tenant_id"]
    )
    storage_key = f"{tenant_key}/{image_name}"
    image_url = _product_image_url(
        normalized_sku,
        image_name,
    )
    record = _product_image_record(
        db,
        user["tenant_id"],
        normalized_sku,
    )
    if (
        record is None
        or record.get("image_url") != image_url
        or record.get("image_storage_key") != storage_key
    ):
        raise image_error(
            "product_image_not_found",
            "Product image not found.",
            status.HTTP_404_NOT_FOUND,
        )

    image_path = _product_image_path(storage_key)
    if not image_path.is_file():
        raise image_error(
            "product_image_not_found",
            "Product image not found.",
            status.HTTP_404_NOT_FOUND,
        )
    return FileResponse(
        image_path,
        media_type="image/webp",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/products/{sku}/image")
def delete_product_image(
    sku: str,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        item, previous_storage_key = (
            replace_product_image_metadata(
                db,
                user,
                sku.strip(),
                image_url=None,
                image_storage_key=None,
            )
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Product image removal failed.",
            "The product image was not removed.",
        )

    _remove_product_image(previous_storage_key)
    return data_response(item)


@router.post("/transactions")
def post_transaction(
    payload: TransactionCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return create_transaction(
            db,
            user,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            (
                "Inventory transaction failed "
                "before completion."
            ),
            (
                "The transaction was not committed. "
                "Inventory remains unchanged."
            ),
        )


@router.get("/transactions")
def get_transaction_history(
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    return data_response(
        transaction_history(
            db,
            user["tenant_id"],
            limit,
        )
    )


@router.post(
    "/transactions/{transaction_id}/reverse"
)
def post_reversal(
    transaction_id: str,
    payload: ReversalCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        return reverse_transaction(
            db,
            user,
            transaction_id,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            (
                "Inventory reversal failed "
                "before completion."
            ),
            (
                "The reversal was not committed. "
                "Inventory remains unchanged."
            ),
        )


@router.get("/transfers")
def get_transfers(
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
    ),
    transfer_status: TransferStatus | None = Query(
        default=None,
        alias="status",
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    return data_response(
        list_transfers(
            db,
            user["tenant_id"],
            limit,
            transfer_status,
        )
    )


@router.post(
    "/transfers",
    status_code=status.HTTP_201_CREATED,
)
def post_transfer(
    payload: TransferCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return create_transfer(
            db,
            user,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Transfer creation failed before completion.",
            (
                "The transfer was not created. "
                "Inventory remains unchanged."
            ),
        )


@router.post(
    "/transfers/{transfer_id}/receive"
)
def post_transfer_receipt(
    transfer_id: str,
    payload: TransferReceive,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return receive_transfer(
            db,
            user,
            transfer_id,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Transfer receipt failed before completion.",
            (
                "The transfer was not confirmed. "
                "Stock locations remain unchanged."
            ),
        )


@router.get("/deliveries")
def get_deliveries(
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    return data_response(
        list_receipts(
            db,
            user["tenant_id"],
            limit,
        )
    )


@router.post(
    "/deliveries",
    status_code=status.HTTP_201_CREATED,
)
def post_delivery(
    payload: ReceiptCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return create_receipt(
            db,
            user,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Delivery receipt failed before completion.",
            (
                "The delivery was not received. "
                "Inventory remains unchanged."
            ),
        )


@router.get("/returns")
def get_returns(
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    return data_response(
        list_returns(
            db,
            user["tenant_id"],
            limit,
        )
    )


@router.post(
    "/returns",
    status_code=status.HTTP_201_CREATED,
)
def post_return(
    payload: ReturnCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return create_return(
            db,
            user,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Return failed before completion.",
            (
                "The return was not recorded. "
                "Inventory remains unchanged."
            ),
        )


@router.get("/damage")
def get_damage(
    limit: int = Query(
        default=200,
        ge=1,
        le=500,
    ),
    damage_status: DamageStatus | None = Query(
        default=None,
        alias="status",
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    return data_response(
        list_damage(
            db,
            user["tenant_id"],
            damage_status,
            limit,
        )
    )


@router.post(
    "/damage",
    status_code=status.HTTP_201_CREATED,
)
def post_damage(
    payload: DamageCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return create_damage(
            db,
            user,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Damage report failed before completion.",
            (
                "The damage was not recorded. "
                "Available stock remains unchanged."
            ),
        )


@router.post("/damage/{damage_id}/resolve")
def post_damage_resolution(
    damage_id: str,
    payload: DamageResolve,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        return resolve_damage(
            db,
            user,
            damage_id,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Damage resolution failed before completion.",
            (
                "The damage report was not resolved. "
                "Inventory remains unchanged."
            ),
        )


@router.get("/investigations")
def get_investigations(
    limit: int = Query(
        default=200,
        ge=1,
        le=500,
    ),
    investigation_status: InvestigationStatus | None = Query(
        default=None,
        alias="status",
    ),
    investigation_kind: InvestigationKind | None = Query(
        default=None,
        alias="kind",
    ),
    location_code: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return data_response(
            list_investigations(
                db,
                user["tenant_id"],
                investigation_status,
                investigation_kind,
                location_code,
                limit,
            )
        )
    except DomainError as error:
        raise_domain_error(error)


@router.post(
    "/investigations",
    status_code=status.HTTP_201_CREATED,
)
def post_investigation(
    payload: InvestigationCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return create_investigation(
            db,
            user,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Investigation creation failed before completion.",
            (
                "The investigation was not opened. "
                "Inventory remains unchanged."
            ),
        )


@router.post(
    "/investigations/{investigation_id}/evidence"
)
def post_investigation_evidence(
    investigation_id: str,
    payload: InvestigationEvidenceCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return add_investigation_evidence(
            db,
            user,
            investigation_id,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Investigation evidence failed before completion.",
            (
                "The check was not recorded. "
                "Investigation history remains unchanged."
            ),
        )


@router.post(
    "/investigations/{investigation_id}/resolve"
)
def post_investigation_resolution(
    investigation_id: str,
    payload: InvestigationResolve,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        return resolve_investigation(
            db,
            user,
            investigation_id,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Investigation resolution failed before completion.",
            (
                "The investigation was not resolved. "
                "Inventory remains unchanged."
            ),
        )


@router.get("/reservations")
def get_reservations(
    limit: int = Query(
        default=200,
        ge=1,
        le=500,
    ),
    reservation_status: ReservationStatus | None = Query(
        default=None,
        alias="status",
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    return data_response(
        list_reservations(
            db,
            user["tenant_id"],
            reservation_status,
            limit,
        )
    )


@router.post(
    "/reservations",
    status_code=status.HTTP_201_CREATED,
)
def post_reservation(
    payload: ReservationCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return create_reservation(
            db,
            user,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Reservation failed before completion.",
            (
                "Stock was not reserved. "
                "Available quantities remain unchanged."
            ),
        )


@router.post(
    "/reservations/{reservation_id}/close"
)
def post_reservation_close(
    reservation_id: str,
    payload: ReservationClose,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return close_reservation(
            db,
            user,
            reservation_id,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Reservation close failed before completion.",
            (
                "The reservation was not closed. "
                "Inventory remains unchanged."
            ),
        )


@router.get("/counts")
def get_stock_counts(
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
    ),
    count_status: StockCountStatus | None = Query(
        default=None,
        alias="status",
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    return data_response(
        list_stock_counts(
            db,
            user["tenant_id"],
            count_status,
            limit,
        )
    )


@router.post(
    "/counts",
    status_code=status.HTTP_201_CREATED,
)
def post_stock_count(
    payload: StockCountCreate,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return create_stock_count(
            db,
            user,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Stock count failed before completion.",
            (
                "The count was not recorded. "
                "Inventory remains unchanged."
            ),
        )


@router.post("/counts/{count_id}/approve")
def post_stock_count_approval(
    count_id: str,
    payload: StockCountApprove,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        return approve_stock_count(
            db,
            user,
            count_id,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Stock count approval failed before completion.",
            (
                "Count corrections were not applied. "
                "Inventory remains unchanged."
            ),
        )


@router.post("/counts/{count_id}/cancel")
def post_stock_count_cancellation(
    count_id: str,
    payload: StockCountCancel,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        return cancel_stock_count(
            db,
            user,
            count_id,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Stock count cancellation failed before completion.",
            "The count remains open.",
        )


@router.get("/orders/comparisons")
def get_order_comparisons(
    limit: int = Query(
        default=50,
        ge=1,
        le=100,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    try:
        return data_response(
            list_order_comparisons(
                db,
                user["tenant_id"],
                limit,
            )
        )
    except DomainError as error:
        raise_domain_error(error)


@router.post("/orders/comparisons/import")
async def post_order_comparison(
    file: UploadFile = File(...),
    sales_channel: str = Form(
        ...,
        min_length=1,
        max_length=80,
    ),
    idempotency_key: str = Form(
        ...,
        min_length=8,
        max_length=128,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    filename = (
        file.filename or ""
    ).strip()
    try:
        if not filename.lower().endswith(".csv"):
            raise import_error(
                "invalid_order_file",
                "Choose a CSV file exported from the selling app.",
            )
        contents = await file.read(
            MAX_IMPORT_BYTES + 1
        )
    finally:
        await file.close()

    if not contents:
        raise import_error(
            "empty_order_file",
            "The order CSV is empty.",
        )
    if len(contents) > MAX_IMPORT_BYTES:
        raise import_error(
            "order_file_too_large",
            "The order CSV must be 2 MB or smaller.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    clean_channel = sales_channel.strip()
    clean_key = idempotency_key.strip()
    if not clean_channel:
        raise import_error(
            "invalid_sales_channel",
            "Enter the selling app name.",
        )
    if not 8 <= len(clean_key) <= 128:
        raise import_error(
            "invalid_idempotency_key",
            "The import key is invalid.",
        )

    try:
        return await run_in_threadpool(
            partial(
                import_selling_orders_csv,
                db,
                user,
                contents,
                sales_channel=clean_channel,
                idempotency_key=clean_key,
                source_filename=filename,
            )
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Selling-order comparison failed before completion.",
            (
                "The order check was not saved. "
                "Inventory remains unchanged."
            ),
        )


@router.post(
    "/orders/comparisons/lines/{line_id}/resolve"
)
def post_order_comparison_resolution(
    line_id: str,
    payload: OrderComparisonResolve,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        return resolve_order_comparison(
            db,
            user,
            line_id,
            payload,
        )
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            "Order-comparison resolution failed before completion.",
            (
                "The discrepancy was not resolved. "
                "Inventory remains unchanged."
            ),
        )


@router.get("/ai/reviews")
def get_ai_reviews(
    review_type: AIReviewType | None = Query(
        default=None,
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=100,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        return data_response(
            list_ai_reviews(
                db,
                user["tenant_id"],
                review_type=review_type,
                limit=limit,
            )
        )
    except AIReviewError as error:
        raise_ai_review_error(error)
    except Exception:
        raise_ai_review_failure(
            "AI review history could not be loaded.",
            "AI review history could not be loaded.",
        )


@router.post("/ai/delivery-document")
async def post_delivery_document_review(
    request: Request,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
    ),
    source_filename: str | None = Header(
        default=None,
        alias="X-Stockpile-Filename",
        max_length=180,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = -1
        if declared_size < 0:
            raise_ai_review_error(
                AIReviewError(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_content_length",
                    "The delivery document size is invalid.",
                )
            )
        if declared_size > MAX_DOCUMENT_BYTES:
            raise_ai_review_error(
                AIReviewError(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    "document_too_large",
                    "Delivery document images must be 5 MB or smaller.",
                )
            )

    contents = bytearray()
    async for chunk in request.stream():
        if len(contents) + len(chunk) > MAX_DOCUMENT_BYTES:
            raise_ai_review_error(
                AIReviewError(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    "document_too_large",
                    "Delivery document images must be 5 MB or smaller.",
                )
            )
        contents.extend(chunk)

    if not contents:
        raise_ai_review_error(
            AIReviewError(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "empty_document",
                "Choose a delivery document image.",
            )
        )

    try:
        return await run_in_threadpool(
            partial(
                create_delivery_document_review,
                db,
                user,
                filename=(source_filename or "delivery-document"),
                content_type=request.headers.get("content-type"),
                content=bytes(contents),
                idempotency_key=idempotency_key,
            )
        )
    except AIReviewError as error:
        raise_ai_review_error(error)
    except Exception:
        raise_ai_review_failure(
            "Delivery document review failed.",
            "The delivery document could not be reviewed.",
        )


@router.post("/ai/discrepancies")
def post_discrepancy_review(
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    try:
        payload = AIDiscrepancyReviewCreate(
            idempotency_key=idempotency_key,
        )
        return create_discrepancy_review(
            db,
            user,
            payload,
        )
    except AIReviewError as error:
        raise_ai_review_error(error)
    except Exception:
        raise_ai_review_failure(
            "Discrepancy review failed.",
            "The discrepancy review could not be completed.",
        )


@router.post("/import")
async def post_inventory_import(
    file: UploadFile = File(...),
    idempotency_key: str = Form(
        ...,
        min_length=8,
        max_length=128,
    ),
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    filename = (
        file.filename or ""
    ).strip()

    if not filename.lower().endswith(".csv"):
        raise import_error(
            "invalid_import_file",
            "Choose a CSV file exported from Google Sheets.",
        )

    try:
        contents = await file.read(
            MAX_IMPORT_BYTES + 1
        )
    finally:
        await file.close()

    if not contents:
        raise import_error(
            "empty_import_file",
            "The CSV file is empty.",
        )

    if len(contents) > MAX_IMPORT_BYTES:
        raise import_error(
            "import_file_too_large",
            "The CSV file must be 2 MB or smaller.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    try:
        csv_text = contents.decode(
            "utf-8-sig"
        )
    except UnicodeDecodeError:
        raise import_error(
            "invalid_import_encoding",
            "Save the CSV file as UTF-8 and try again.",
        )

    if not csv_text.strip():
        raise import_error(
            "empty_import_file",
            "The CSV file is empty.",
        )

    clean_key = idempotency_key.strip()

    if not 8 <= len(clean_key) <= 128:
        raise import_error(
            "invalid_idempotency_key",
            "The import key is invalid.",
        )

    try:
        result = import_inventory_csv(
            db,
            user,
            csv_text,
            clean_key,
        )

        return data_response(result)
    except DomainError as error:
        raise_domain_error(error)
    except Exception:
        raise_write_failure(
            (
                "Inventory import failed "
                "before completion."
            ),
            (
                "Nothing was imported. "
                "Inventory remains unchanged."
            ),
        )


@router.get("/exchange")
def get_exchange_status(
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_operator
    ),
):
    result = list_exchange_records(
        db,
        user["tenant_id"],
    )
    result["mode"] = "one_way_append"
    result["authority"] = AUTHORITY
    return result


@router.post("/exchange/retry")
def retry_exchange(
    payload: ExchangeRetryRequest,
    db: Any = Depends(get_db),
    user: dict[str, Any] = Depends(
        require_admin
    ),
):
    result = retry_exchange_records(
        db,
        user["tenant_id"],
        payload.transaction_id,
    )
    result["mode"] = "one_way_append"
    result["authority"] = AUTHORITY
    return result
