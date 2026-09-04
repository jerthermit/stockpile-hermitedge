import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from config import (
    google_service_account_file,
    google_service_account_json,
    sheets_range,
    sheets_spreadsheet_id,
)
from database import begin_write, execute_write, fetchall, fetchone, utc_now


logger = logging.getLogger("stockpile.sheets")

EXCHANGE_CLAIM_LEASE_SECONDS = 60
SHEET_HEADERS = (
    "transaction_id",
    "created_at",
    "sku",
    "barcode",
    "product_name",
    "location_code",
    "location_name",
    "location_path",
    "movement_type",
    "quantity",
    "quantity_delta",
    "location_balance_before",
    "location_balance_after",
    "total_balance_before",
    "total_balance_after",
    "operator",
    "reason",
    "source",
    "reverses_transaction_id",
    "authority",
)


class SheetsNotConfigured(RuntimeError):
    pass


def _sheet_value(value: Any) -> Any:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else value


def _transaction_sheet_row(transaction: dict[str, Any]) -> list[Any]:
    return [
        transaction["id"],
        _sheet_value(transaction["created_at"]),
        transaction["sku"],
        transaction["barcode"],
        transaction.get("product_name", ""),
        transaction["location_code"],
        transaction["location_name"],
        transaction["location_path"],
        transaction["movement_type"],
        transaction["quantity"],
        transaction["quantity_delta"],
        transaction["location_balance_before"],
        transaction["location_balance_after"],
        transaction["balance_before"],
        transaction["balance_after"],
        transaction["actor_name"],
        transaction.get("reason") or "",
        transaction["source"],
        transaction.get("reverses_transaction_id") or "",
        "Stockpile application database",
    ]


def append_transaction_to_sheet(transaction: dict[str, Any]) -> None:
    spreadsheet_id = sheets_spreadsheet_id()
    account_file = google_service_account_file()
    account_json = google_service_account_json()
    if not spreadsheet_id or not (account_file or account_json):
        raise SheetsNotConfigured("Google Sheets is not configured.")

    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if account_json:
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(account_json), scopes=scopes
        )
    else:
        credentials = service_account.Credentials.from_service_account_file(
            str(Path(account_file).expanduser()), scopes=scopes
        )

    encoded_range = quote(sheets_range(), safe="!:$")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/"
        f"values/{encoded_range}:append"
    )
    session = AuthorizedSession(credentials)
    try:
        response = session.post(
            url,
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={
                "majorDimension": "ROWS",
                "values": [_transaction_sheet_row(transaction)],
            },
            timeout=15,
        )
        try:
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Google Sheets append returned HTTP {response.status_code}."
                )
        finally:
            response.close()
    finally:
        session.close()


def _transaction_for_exchange(
    db: Any, tenant: str, transaction_id: str, lock: bool = False
) -> dict[str, Any] | None:
    postgres_suffix = " FOR UPDATE OF o" if lock else ""
    return fetchone(
        db,
        """
        SELECT t.*, i.product_name, o.id AS outbox_id, o.status AS exchange_status,
               o.attempts AS exchange_attempts, o.last_error AS exchange_error,
               o.last_attempt_at, o.synced_at, o.claim_token, o.claimed_at
        FROM inventory_transactions t
        JOIN inventory i ON i.tenant_id = t.tenant_id AND i.sku = t.sku
        JOIN sheets_outbox o ON o.transaction_id = t.id
        WHERE t.tenant_id = ? AND t.id = ?
        """,
        (tenant, transaction_id),
        (
            "SELECT t.*, i.product_name, o.id AS outbox_id, "
            "o.status AS exchange_status, o.attempts AS exchange_attempts, "
            "o.last_error AS exchange_error, o.last_attempt_at, o.synced_at, "
            "o.claim_token, o.claimed_at "
            "FROM inventory_transactions t "
            "JOIN inventory i ON i.tenant_id = t.tenant_id AND i.sku = t.sku "
            "JOIN sheets_outbox o ON o.transaction_id = t.id "
            "WHERE t.tenant_id = %s AND t.id = %s" + postgres_suffix
        ),
    )


def exchange_public_state(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "transaction_id": record["id"],
        "status": record["exchange_status"],
        "attempts": record["exchange_attempts"],
        "last_error": record.get("exchange_error"),
        "last_attempt_at": record.get("last_attempt_at"),
        "synced_at": record.get("synced_at"),
        "location_code": record.get("location_code"),
        "location_name": record.get("location_name"),
        "location_path": record.get("location_path"),
    }


def _claim_is_active(record: dict[str, Any]) -> bool:
    if not record.get("claim_token") or not record.get("claimed_at"):
        return False

    claimed_at = record["claimed_at"]
    if isinstance(claimed_at, str):
        try:
            claimed_at = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - claimed_at < timedelta(
        seconds=EXCHANGE_CLAIM_LEASE_SECONDS
    )


def attempt_transaction_exchange(
    db: Any, tenant: str, transaction_id: str
) -> dict[str, Any]:
    claim_token = str(uuid.uuid4())
    attempted_at = utc_now()
    try:
        begin_write(db)
        record = _transaction_for_exchange(db, tenant, transaction_id, lock=True)
        if record is None:
            db.rollback()
            raise LookupError("Exchange record not found.")
        if record["exchange_status"] == "synced" or _claim_is_active(record):
            db.commit()
            return exchange_public_state(record)

        execute_write(
            db,
            """
            UPDATE sheets_outbox
            SET claim_token = ?, claimed_at = ?, last_attempt_at = ?
            WHERE id = ?
            """,
            (claim_token, attempted_at, attempted_at, record["outbox_id"]),
            """
            UPDATE sheets_outbox
            SET claim_token = %s, claimed_at = %s, last_attempt_at = %s
            WHERE id = %s
            """,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    outcome = "synced"
    error_message: str | None = None
    try:
        append_transaction_to_sheet(record)
    except SheetsNotConfigured as exc:
        outcome = "not_configured"
        error_message = str(exc)
    except Exception as exc:
        outcome = "failed"
        error_message = str(exc)[:500]
        logger.warning("Sheets export failed for transaction %s", transaction_id)

    completed_at = utc_now()
    try:
        begin_write(db)
        current = _transaction_for_exchange(db, tenant, transaction_id, lock=True)
        if current is None:
            db.rollback()
            raise LookupError("Exchange record not found after delivery attempt.")
        if current.get("claim_token") != claim_token:
            db.commit()
            return exchange_public_state(current)

        if outcome == "not_configured":
            rowcount = execute_write(
                db,
                """
                UPDATE sheets_outbox
                SET status = 'not_configured', last_error = ?, last_attempt_at = ?,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = ? AND claim_token = ?
                """,
                (error_message, attempted_at, record["outbox_id"], claim_token),
                """
                UPDATE sheets_outbox
                SET status = 'not_configured', last_error = %s, last_attempt_at = %s,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = %s AND claim_token = %s
                """,
            )
        elif outcome == "failed":
            rowcount = execute_write(
                db,
                """
                UPDATE sheets_outbox
                SET status = 'failed', attempts = attempts + 1,
                    last_error = ?, last_attempt_at = ?,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = ? AND claim_token = ?
                """,
                (error_message, attempted_at, record["outbox_id"], claim_token),
                """
                UPDATE sheets_outbox
                SET status = 'failed', attempts = attempts + 1,
                    last_error = %s, last_attempt_at = %s,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = %s AND claim_token = %s
                """,
            )
        else:
            rowcount = execute_write(
                db,
                """
                UPDATE sheets_outbox
                SET status = 'synced', attempts = attempts + 1, last_error = NULL,
                    last_attempt_at = ?, synced_at = ?,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = ? AND claim_token = ?
                """,
                (attempted_at, completed_at, record["outbox_id"], claim_token),
                """
                UPDATE sheets_outbox
                SET status = 'synced', attempts = attempts + 1, last_error = NULL,
                    last_attempt_at = %s, synced_at = %s,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = %s AND claim_token = %s
                """,
            )

        if rowcount != 1:
            db.rollback()
            current = _transaction_for_exchange(db, tenant, transaction_id)
            if current is None:
                raise LookupError("Exchange record not found after claim changed.")
            return exchange_public_state(current)
        db.commit()
    except Exception:
        db.rollback()
        raise

    refreshed = _transaction_for_exchange(db, tenant, transaction_id)
    if refreshed is None:
        raise LookupError("Exchange record not found after update.")
    return exchange_public_state(refreshed)


def list_exchange_records(db: Any, tenant: str, limit: int = 100) -> dict[str, Any]:
    rows = fetchall(
        db,
        """
        SELECT o.transaction_id, o.status, o.attempts, o.last_error,
               o.last_attempt_at, o.synced_at, o.created_at,
               t.sku, t.barcode, t.movement_type, t.quantity, t.actor_name,
               t.location_code, t.location_name, t.location_path
        FROM sheets_outbox o
        JOIN inventory_transactions t ON t.id = o.transaction_id
        WHERE o.tenant_id = ?
        ORDER BY o.created_at DESC
        LIMIT ?
        """,
        (tenant, limit),
        """
        SELECT o.transaction_id, o.status, o.attempts, o.last_error,
               o.last_attempt_at, o.synced_at, o.created_at,
               t.sku, t.barcode, t.movement_type, t.quantity, t.actor_name,
               t.location_code, t.location_name, t.location_path
        FROM sheets_outbox o
        JOIN inventory_transactions t ON t.id = o.transaction_id
        WHERE o.tenant_id = %s
        ORDER BY o.created_at DESC
        LIMIT %s
        """,
    )
    summary = {"pending": 0, "synced": 0, "failed": 0, "not_configured": 0}
    for row in rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    return {"summary": summary, "data": rows}


def retry_exchange_records(
    db: Any, tenant: str, transaction_id: str | None = None
) -> dict[str, Any]:
    if transaction_id:
        candidates = [{"transaction_id": transaction_id}]
    else:
        candidates = fetchall(
            db,
            """
            SELECT transaction_id FROM sheets_outbox
            WHERE tenant_id = ? AND status IN ('pending', 'failed', 'not_configured')
            ORDER BY created_at ASC LIMIT 25
            """,
            (tenant,),
            """
            SELECT transaction_id FROM sheets_outbox
            WHERE tenant_id = %s AND status IN ('pending', 'failed', 'not_configured')
            ORDER BY created_at ASC LIMIT 25
            """,
        )

    results = []
    for candidate in candidates:
        try:
            results.append(
                attempt_transaction_exchange(db, tenant, candidate["transaction_id"])
            )
        except LookupError:
            continue
    current = list_exchange_records(db, tenant)
    current["retried"] = results
    return current
