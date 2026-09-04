import csv
import hashlib
import io
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from database import (
    INTEGRITY_ERRORS,
    badge_code_digest,
    begin_write,
    execute_write,
    fetchall,
    fetchone,
    utc_now,
)
from schemas import (
    DamageCreate,
    DamageResolve,
    InvestigationCreate,
    InvestigationEvidenceCreate,
    InvestigationResolve,
    OrderComparisonResolve,
    ProductCreate,
    ProductReceiveCreate,
    ProductUpdate,
    ReceiptCreate,
    ReservationClose,
    ReservationCreate,
    ReversalCreate,
    ReturnCreate,
    SellingOrderImport,
    SellingOrderLine,
    StockCountApprove,
    StockCountCancel,
    StockCountCreate,
    TransactionCreate,
    TransferCreate,
    TransferReceive,
)
from security import verify_password
from services.sheets import attempt_transaction_exchange, exchange_public_state


logger = logging.getLogger("stockpile.inventory")


@dataclass
class DomainError(Exception):
    status_code: int
    code: str
    detail: str


@dataclass(frozen=True)
class ImportRow:
    line_number: int
    product: ProductCreate
    location_code: str
    quantity: int
    reason: str | None
    provided_fields: frozenset[str]


@dataclass(frozen=True)
class SellingOrderCsvRow:
    line_number: int
    order: SellingOrderLine


def inventory_status(stock: int) -> str:
    return "in_stock" if stock > 0 else "out_of_stock"


def _location_rows(db: Any, tenant: str) -> list[dict[str, Any]]:
    return fetchall(
        db,
        """
        SELECT code, name, kind, parent_code, active, stockable
        FROM locations WHERE tenant_id = ?
        ORDER BY code
        """,
        (tenant,),
        """
        SELECT code, name, kind, parent_code, active, stockable
        FROM locations WHERE tenant_id = %s
        ORDER BY code
        """,
    )


def _location_paths(rows: list[dict[str, Any]]) -> dict[str, str]:
    by_code = {row["code"]: row for row in rows}
    paths: dict[str, str] = {}
    for code in by_code:
        names: list[str] = []
        current_code: str | None = code
        visited: set[str] = set()
        while current_code and current_code not in visited:
            visited.add(current_code)
            current = by_code.get(current_code)
            if current is None:
                break
            names.append(current["name"])
            current_code = current.get("parent_code")
        paths[code] = " / ".join(reversed(names))
    return paths


def list_locations(db: Any, tenant: str) -> list[dict[str, Any]]:
    rows = _location_rows(db, tenant)
    paths = _location_paths(rows)
    return [
        {
            "code": row["code"],
            "name": row["name"],
            "kind": row["kind"],
            "parent_code": row.get("parent_code"),
            "path": paths[row["code"]],
            "active": bool(row["active"]),
            "stockable": bool(row["stockable"]),
        }
        for row in rows
    ]


def _stock_controls_by_position(
    db: Any,
    tenant: str,
) -> dict[tuple[str, str], dict[str, int]]:
    controls: dict[tuple[str, str], dict[str, int]] = {}
    reserved_rows = fetchall(
        db,
        """
        SELECT line.sku, line.location_code,
               COALESCE(SUM(line.quantity), 0) AS quantity
        FROM inventory_reservation_lines line
        JOIN inventory_reservations reservation
          ON reservation.tenant_id = line.tenant_id
         AND reservation.id = line.reservation_id
        WHERE line.tenant_id = ? AND reservation.status = 'active'
        GROUP BY line.sku, line.location_code
        """,
        (tenant,),
        """
        SELECT line.sku, line.location_code,
               COALESCE(SUM(line.quantity), 0) AS quantity
        FROM inventory_reservation_lines line
        JOIN inventory_reservations reservation
          ON reservation.tenant_id = line.tenant_id
         AND reservation.id = line.reservation_id
        WHERE line.tenant_id = %s AND reservation.status = 'active'
        GROUP BY line.sku, line.location_code
        """,
    )
    for row in reserved_rows:
        controls.setdefault(
            (row["sku"], row["location_code"]),
            {"reserved": 0, "damaged": 0},
        )["reserved"] = int(row["quantity"])

    damaged_rows = fetchall(
        db,
        """
        SELECT sku, location_code,
               COALESCE(SUM(remaining_quantity), 0) AS quantity
        FROM inventory_damage
        WHERE tenant_id = ? AND status = 'open'
        GROUP BY sku, location_code
        """,
        (tenant,),
        """
        SELECT sku, location_code,
               COALESCE(SUM(remaining_quantity), 0) AS quantity
        FROM inventory_damage
        WHERE tenant_id = %s AND status = 'open'
        GROUP BY sku, location_code
        """,
    )
    for row in damaged_rows:
        controls.setdefault(
            (row["sku"], row["location_code"]),
            {"reserved": 0, "damaged": 0},
        )["damaged"] = int(row["quantity"])
    return controls


def _positions_by_sku(db: Any, tenant: str) -> dict[str, list[dict[str, Any]]]:
    location_rows = _location_rows(db, tenant)
    paths = _location_paths(location_rows)
    locations = {row["code"]: row for row in location_rows}
    rows = fetchall(
        db,
        """
        SELECT sku, location_code, quantity, last_updated, version
        FROM inventory_positions
        WHERE tenant_id = ? AND quantity > 0
        ORDER BY location_code, sku
        """,
        (tenant,),
        """
        SELECT sku, location_code, quantity, last_updated, version
        FROM inventory_positions
        WHERE tenant_id = %s AND quantity > 0
        ORDER BY location_code, sku
        """,
    )
    controls = _stock_controls_by_position(db, tenant)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        location = locations.get(row["location_code"], {})
        control = controls.get(
            (row["sku"], row["location_code"]),
            {"reserved": 0, "damaged": 0},
        )
        reserved = int(control["reserved"])
        damaged = int(control["damaged"])
        available = max(int(row["quantity"]) - reserved - damaged, 0)
        grouped.setdefault(row["sku"], []).append(
            {
                "location_code": row["location_code"],
                "location_name": location.get("name", row["location_code"]),
                "location_path": paths.get(row["location_code"], row["location_code"]),
                "quantity": row["quantity"],
                "reserved_quantity": reserved,
                "damaged_quantity": damaged,
                "available_quantity": available,
                "last_updated": row["last_updated"],
                "version": row.get("version", 0),
            }
        )
    return grouped


def inventory_public(
    item: dict[str, Any],
    positions: list[dict[str, Any]] | None = None,
    *,
    include_acquisition_cost: bool = False,
) -> dict[str, Any]:
    public_positions = positions or []
    reserved_stock = sum(
        int(position.get("reserved_quantity", 0))
        for position in public_positions
    )
    damaged_stock = sum(
        int(position.get("damaged_quantity", 0))
        for position in public_positions
    )
    available_stock = max(
        int(item["current_stock"]) - reserved_stock - damaged_stock,
        0,
    )
    result = {
        "sku": item["sku"],
        "barcode": item["barcode"],
        "product_name": item["product_name"],
        "category": item.get("category") or "General",
        "unit": item.get("unit") or "unit",
        "variant": item.get("variant"),
        "image_url": item.get("image_url"),
        "track_individually": bool(item.get("track_individually", False)),
        "active": bool(item.get("active", True)),
        "current_stock": item["current_stock"],
        "reserved_stock": reserved_stock,
        "damaged_stock": damaged_stock,
        "available_stock": available_stock,
        "status": item.get("status") or inventory_status(item["current_stock"]),
        "last_updated": item.get("last_updated"),
        "version": item.get("version", 0),
        "positions": public_positions,
    }
    if include_acquisition_cost:
        result["acquisition_cost_centavos"] = item.get(
            "acquisition_cost_centavos"
        )
    return result


def list_inventory(
    db: Any,
    tenant: str,
    *,
    include_acquisition_cost: bool = False,
) -> list[dict[str, Any]]:
    rows = fetchall(
        db,
        """
        SELECT sku, barcode, product_name, category, unit, variant,
               acquisition_cost_centavos, image_url,
               track_individually, active, current_stock, status,
               last_updated, version
        FROM inventory WHERE tenant_id = ?
        ORDER BY active DESC, product_name, sku
        """,
        (tenant,),
        """
        SELECT sku, barcode, product_name, category, unit, variant,
               acquisition_cost_centavos, image_url,
               track_individually, active, current_stock, status,
               last_updated, version
        FROM inventory WHERE tenant_id = %s
        ORDER BY active DESC, product_name, sku
        """,
    )
    positions = _positions_by_sku(db, tenant)
    return [
        inventory_public(
            row,
            positions.get(row["sku"], []),
            include_acquisition_cost=include_acquisition_cost,
        )
        for row in rows
    ]


def find_by_barcode(
    db: Any,
    tenant: str,
    barcode: str,
    *,
    include_acquisition_cost: bool = False,
) -> dict[str, Any] | None:
    row = fetchone(
        db,
        """
        SELECT sku, barcode, product_name, category, unit, variant,
               acquisition_cost_centavos, image_url,
               track_individually, active, current_stock, status,
               last_updated, version
        FROM inventory WHERE tenant_id = ? AND barcode = ?
        """,
        (tenant, barcode),
        """
        SELECT sku, barcode, product_name, category, unit, variant,
               acquisition_cost_centavos, image_url,
               track_individually, active, current_stock, status,
               last_updated, version
        FROM inventory WHERE tenant_id = %s AND barcode = %s
        """,
    )
    if row is None:
        return None
    positions = _positions_by_sku(db, tenant)
    return inventory_public(
        row,
        positions.get(row["sku"], []),
        include_acquisition_cost=include_acquisition_cost,
    )


def _load_transaction(
    db: Any,
    tenant: str,
    *,
    transaction_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    if transaction_id:
        sqlite_where = "t.tenant_id = ? AND t.id = ?"
        postgres_where = "t.tenant_id = %s AND t.id = %s"
        params = (tenant, transaction_id)
    else:
        sqlite_where = "t.tenant_id = ? AND t.idempotency_key = ?"
        postgres_where = "t.tenant_id = %s AND t.idempotency_key = %s"
        params = (tenant, idempotency_key)

    return fetchone(
        db,
        f"""
        SELECT t.*, i.product_name, i.category,
               o.status AS exchange_status, o.attempts AS exchange_attempts,
               o.last_error AS exchange_error, o.last_attempt_at, o.synced_at
        FROM inventory_transactions t
        JOIN inventory i ON i.tenant_id = t.tenant_id AND i.sku = t.sku
        JOIN sheets_outbox o ON o.transaction_id = t.id
        WHERE {sqlite_where}
        """,
        params,
        f"""
        SELECT t.*, i.product_name, i.category,
               o.status AS exchange_status, o.attempts AS exchange_attempts,
               o.last_error AS exchange_error, o.last_attempt_at, o.synced_at
        FROM inventory_transactions t
        JOIN inventory i ON i.tenant_id = t.tenant_id AND i.sku = t.sku
        JOIN sheets_outbox o ON o.transaction_id = t.id
        WHERE {postgres_where}
        """,
    )


def transaction_public(transaction: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": transaction["id"],
        "sku": transaction["sku"],
        "barcode": transaction["barcode"],
        "product_name": transaction.get("product_name"),
        "category": transaction.get("category"),
        "movement_type": transaction["movement_type"],
        "quantity": transaction["quantity"],
        "quantity_delta": transaction["quantity_delta"],
        "balance_before": transaction["balance_before"],
        "balance_after": transaction["balance_after"],
        "actor_user_id": transaction["actor_user_id"],
        "actor_name": transaction["actor_name"],
        "location_code": transaction["location_code"],
        "location_name": transaction["location_name"],
        "location_path": transaction["location_path"],
        "location_balance_before": transaction["location_balance_before"],
        "location_balance_after": transaction["location_balance_after"],
        "reason": transaction.get("reason"),
        "source": transaction["source"],
        "created_at": transaction["created_at"],
        "reverses_transaction_id": transaction.get("reverses_transaction_id"),
        "reversed_by_transaction_id": transaction.get("reversed_by_transaction_id"),
        "exchange_status": transaction["exchange_status"],
    }


def _existing_create_matches(
    existing: dict[str, Any], payload: TransactionCreate
) -> bool:
    return (
        existing["barcode"] == payload.barcode
        and existing["movement_type"] == payload.movement_type
        and existing["quantity"] == payload.quantity
        and existing["location_code"] == payload.location_code
        and (existing.get("reason") or None) == payload.reason
        and existing["source"] == payload.source
    )


def _existing_product_create_matches(
    existing: dict[str, Any], payload: ProductReceiveCreate
) -> bool:
    return (
        existing["sku"] == payload.sku
        and existing["barcode"] == payload.barcode
        and existing["movement_type"] == "stock_in"
        and existing["quantity"] == payload.quantity
        and existing["location_code"] == payload.location_code
        and (existing.get("reason") or None) == (payload.reason or "Opening stock")
        and existing["source"] == "manual"
    )


def _existing_reversal_matches(
    existing: dict[str, Any], original_transaction_id: str, payload: ReversalCreate
) -> bool:
    return (
        existing.get("reverses_transaction_id") == original_transaction_id
        and (existing.get("reason") or None) == payload.reason
    )


def _item_for_write(db: Any, tenant: str, barcode: str) -> dict[str, Any] | None:
    return fetchone(
        db,
        "SELECT * FROM inventory WHERE tenant_id = ? AND barcode = ?",
        (tenant, barcode),
        "SELECT * FROM inventory WHERE tenant_id = %s AND barcode = %s FOR UPDATE",
    )


def _item_by_sku_for_write(db: Any, tenant: str, sku: str) -> dict[str, Any] | None:
    return fetchone(
        db,
        "SELECT * FROM inventory WHERE tenant_id = ? AND sku = ?",
        (tenant, sku),
        "SELECT * FROM inventory WHERE tenant_id = %s AND sku = %s FOR UPDATE",
    )


def _location_for_write(
    db: Any,
    tenant: str,
    code: str,
    *,
    require_active_stockable: bool = True,
) -> dict[str, Any]:
    rows = _location_rows(db, tenant)
    by_code = {row["code"]: row for row in rows}
    location = by_code.get(code)
    if location is None:
        raise DomainError(404, "location_not_found", "Location not found.")
    if require_active_stockable and not bool(location["active"]):
        raise DomainError(409, "inactive_location", "This location is inactive.")
    if require_active_stockable and not bool(location["stockable"]):
        raise DomainError(
            409,
            "location_not_stockable",
            "Choose a rack, bin, receiving area, or another stock location.",
        )
    location["path"] = _location_paths(rows)[code]
    return location


def _position_for_write(
    db: Any,
    tenant: str,
    sku: str,
    location_code: str,
    timestamp: str,
) -> dict[str, Any]:
    execute_write(
        db,
        """
        INSERT INTO inventory_positions
            (tenant_id, sku, location_code, quantity, last_updated, version)
        VALUES (?, ?, ?, 0, ?, 0)
        ON CONFLICT(tenant_id, sku, location_code) DO NOTHING
        """,
        (tenant, sku, location_code, timestamp),
        """
        INSERT INTO inventory_positions
            (tenant_id, sku, location_code, quantity, last_updated, version)
        VALUES (%s, %s, %s, 0, %s, 0)
        ON CONFLICT(tenant_id, sku, location_code) DO NOTHING
        """,
    )
    position = fetchone(
        db,
        """
        SELECT * FROM inventory_positions
        WHERE tenant_id = ? AND sku = ? AND location_code = ?
        """,
        (tenant, sku, location_code),
        """
        SELECT * FROM inventory_positions
        WHERE tenant_id = %s AND sku = %s AND location_code = %s
        FOR UPDATE
        """,
    )
    if position is None:
        raise RuntimeError("Inventory position could not be loaded.")
    return position


def _position_commitments(
    db: Any,
    tenant: str,
    sku: str,
    location_code: str,
    on_hand: int,
) -> dict[str, int]:
    reserved_row = fetchone(
        db,
        """
        SELECT COALESCE(SUM(line.quantity), 0) AS quantity
        FROM inventory_reservation_lines line
        JOIN inventory_reservations reservation
          ON reservation.tenant_id = line.tenant_id
         AND reservation.id = line.reservation_id
        WHERE line.tenant_id = ? AND line.sku = ?
          AND line.location_code = ? AND reservation.status = 'active'
        """,
        (tenant, sku, location_code),
        """
        SELECT COALESCE(SUM(line.quantity), 0) AS quantity
        FROM inventory_reservation_lines line
        JOIN inventory_reservations reservation
          ON reservation.tenant_id = line.tenant_id
         AND reservation.id = line.reservation_id
        WHERE line.tenant_id = %s AND line.sku = %s
          AND line.location_code = %s AND reservation.status = 'active'
        """,
    )
    damaged_row = fetchone(
        db,
        """
        SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity
        FROM inventory_damage
        WHERE tenant_id = ? AND sku = ? AND location_code = ?
          AND status = 'open'
        """,
        (tenant, sku, location_code),
        """
        SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity
        FROM inventory_damage
        WHERE tenant_id = %s AND sku = %s AND location_code = %s
          AND status = 'open'
        """,
    )
    reserved = int((reserved_row or {}).get("quantity") or 0)
    damaged = int((damaged_row or {}).get("quantity") or 0)
    available = int(on_hand) - reserved - damaged
    if available < 0:
        raise DomainError(
            409,
            "inventory_commitment_mismatch",
            "Reserved and damaged quantities exceed physical stock. Review this location.",
        )
    return {
        "reserved": reserved,
        "damaged": damaged,
        "available": available,
    }


def _require_available_stock(
    db: Any,
    tenant: str,
    item: dict[str, Any],
    position: dict[str, Any],
    quantity: int,
    *,
    allowance: int = 0,
) -> dict[str, int]:
    commitments = _position_commitments(
        db,
        tenant,
        item["sku"],
        position["location_code"],
        int(position["quantity"]),
    )
    usable = commitments["available"] + max(int(allowance), 0)
    if quantity > usable:
        raise DomainError(
            409,
            "insufficient_available_stock",
            f"Only {usable} available units remain at this location.",
        )
    return commitments


def _update_balance(
    db: Any, tenant: str, item: dict[str, Any], new_stock: int, timestamp: str
) -> None:
    rowcount = execute_write(
        db,
        """
        UPDATE inventory
        SET current_stock = ?, status = ?, last_updated = ?, version = version + 1
        WHERE tenant_id = ? AND sku = ? AND current_stock = ? AND version = ?
        """,
        (
            new_stock,
            inventory_status(new_stock),
            timestamp,
            tenant,
            item["sku"],
            item["current_stock"],
            item.get("version", 0),
        ),
        """
        UPDATE inventory
        SET current_stock = %s, status = %s, last_updated = %s, version = version + 1
        WHERE tenant_id = %s AND sku = %s AND current_stock = %s AND version = %s
        """,
    )
    if rowcount != 1:
        raise DomainError(
            409,
            "inventory_conflict",
            "Inventory changed while this transaction was being processed. Review and retry.",
        )


def _update_position_balance(
    db: Any,
    tenant: str,
    position: dict[str, Any],
    new_quantity: int,
    timestamp: str,
) -> None:
    rowcount = execute_write(
        db,
        """
        UPDATE inventory_positions
        SET quantity = ?, last_updated = ?, version = version + 1
        WHERE tenant_id = ? AND sku = ? AND location_code = ?
          AND quantity = ? AND version = ?
        """,
        (
            new_quantity,
            timestamp,
            tenant,
            position["sku"],
            position["location_code"],
            position["quantity"],
            position.get("version", 0),
        ),
        """
        UPDATE inventory_positions
        SET quantity = %s, last_updated = %s, version = version + 1
        WHERE tenant_id = %s AND sku = %s AND location_code = %s
          AND quantity = %s AND version = %s
        """,
    )
    if rowcount != 1:
        raise DomainError(
            409,
            "inventory_conflict",
            "Stock at this location changed. Review and retry.",
        )


def _assert_position_total(db: Any, tenant: str, sku: str, expected_total: int) -> None:
    row = fetchone(
        db,
        """
        SELECT COALESCE(SUM(quantity), 0) AS position_total
        FROM inventory_positions WHERE tenant_id = ? AND sku = ?
        """,
        (tenant, sku),
        """
        SELECT COALESCE(SUM(quantity), 0) AS position_total
        FROM inventory_positions WHERE tenant_id = %s AND sku = %s
        """,
    )
    if row is None or int(row["position_total"]) != expected_total:
        raise DomainError(
            409,
            "inventory_balance_mismatch",
            "The item total does not match its locations. No changes were saved.",
        )


def _insert_transaction(
    db: Any,
    *,
    transaction_id: str,
    tenant: str,
    idempotency_key: str,
    item: dict[str, Any],
    movement_type: str,
    quantity: int,
    quantity_delta: int,
    balance_before: int,
    balance_after: int,
    location: dict[str, Any],
    location_balance_before: int,
    location_balance_after: int,
    user: dict[str, Any],
    reason: str | None,
    source: str,
    timestamp: str,
    reverses_transaction_id: str | None = None,
    exchange_status: str = "pending",
) -> None:
    execute_write(
        db,
        """
        INSERT INTO inventory_transactions
            (id, tenant_id, idempotency_key, sku, barcode, movement_type,
             quantity, quantity_delta, balance_before, balance_after,
             actor_user_id, actor_name, location_code, location_name,
             location_path, location_balance_before, location_balance_after,
             reason, source, created_at,
             reverses_transaction_id, reversed_by_transaction_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            transaction_id,
            tenant,
            idempotency_key,
            item["sku"],
            item["barcode"],
            movement_type,
            quantity,
            quantity_delta,
            balance_before,
            balance_after,
            user["id"],
            user["display_name"],
            location["code"],
            location["name"],
            location["path"],
            location_balance_before,
            location_balance_after,
            reason,
            source,
            timestamp,
            reverses_transaction_id,
        ),
        """
        INSERT INTO inventory_transactions
            (id, tenant_id, idempotency_key, sku, barcode, movement_type,
             quantity, quantity_delta, balance_before, balance_after,
             actor_user_id, actor_name, location_code, location_name,
             location_path, location_balance_before, location_balance_after,
             reason, source, created_at,
             reverses_transaction_id, reversed_by_transaction_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
        """,
    )
    execute_write(
        db,
        """
        INSERT INTO sheets_outbox
            (id, tenant_id, transaction_id, status, attempts, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (str(uuid.uuid4()), tenant, transaction_id, exchange_status, timestamp),
        """
        INSERT INTO sheets_outbox
            (id, tenant_id, transaction_id, status, attempts, created_at)
        VALUES (%s, %s, %s, %s, 0, %s)
        """,
    )


def _response_for(
    db: Any,
    tenant: str,
    transaction: dict[str, Any],
    replayed: bool,
    *,
    include_acquisition_cost: bool = False,
) -> dict[str, Any]:
    item = _public_item_by_sku(
        db,
        tenant,
        transaction["sku"],
        include_acquisition_cost=include_acquisition_cost,
    )
    return {
        "authority": "Stockpile application database",
        "data": transaction_public(transaction),
        "inventory": item,
        "exchange": exchange_public_state(transaction),
        "replayed": replayed,
    }


def create_transaction(
    db: Any, user: dict[str, Any], payload: TransactionCreate
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    existing = _load_transaction(db, tenant, idempotency_key=payload.idempotency_key)
    if existing:
        if not _existing_create_matches(existing, payload):
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for a different transaction.",
            )
        return _response_for(
            db,
            tenant,
            existing,
            True,
            include_acquisition_cost=user.get("role") == "admin",
        )

    transaction_id = str(uuid.uuid4())
    timestamp = utc_now()
    try:
        begin_write(db)
        existing = _load_transaction(
            db, tenant, idempotency_key=payload.idempotency_key
        )
        if existing:
            db.commit()
            if not _existing_create_matches(existing, payload):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for a different transaction.",
                )
            return _response_for(
                db,
                tenant,
                existing,
                True,
                include_acquisition_cost=user.get("role") == "admin",
            )

        item = _item_for_write(db, tenant, payload.barcode)
        if item is None:
            raise DomainError(
                404, "unknown_barcode", "No product matches this barcode."
            )
        if not bool(item.get("active", True)):
            raise DomainError(409, "inactive_product", "This product is archived.")

        location = _location_for_write(db, tenant, payload.location_code)
        position = _position_for_write(
            db, tenant, item["sku"], payload.location_code, timestamp
        )
        _assert_position_total(db, tenant, item["sku"], item["current_stock"])

        if payload.movement_type == "stock_out":
            _require_available_stock(
                db,
                tenant,
                item,
                position,
                payload.quantity,
            )

        quantity_delta = (
            payload.quantity
            if payload.movement_type == "stock_in"
            else -payload.quantity
        )
        new_location_stock = position["quantity"] + quantity_delta
        if new_location_stock < 0:
            raise DomainError(
                409,
                "negative_location_inventory",
                f"Only {position['quantity']} units are available at {location['name']}.",
            )
        new_stock = item["current_stock"] + quantity_delta
        if new_stock < 0:
            raise DomainError(
                409,
                "negative_inventory",
                f"Only {item['current_stock']} units are available. Stock cannot become negative.",
            )

        _update_position_balance(db, tenant, position, new_location_stock, timestamp)
        _update_balance(db, tenant, item, new_stock, timestamp)
        _assert_position_total(db, tenant, item["sku"], new_stock)
        _insert_transaction(
            db,
            transaction_id=transaction_id,
            tenant=tenant,
            idempotency_key=payload.idempotency_key,
            item=item,
            movement_type=payload.movement_type,
            quantity=payload.quantity,
            quantity_delta=quantity_delta,
            balance_before=item["current_stock"],
            balance_after=new_stock,
            location=location,
            location_balance_before=position["quantity"],
            location_balance_after=new_location_stock,
            user=user,
            reason=payload.reason,
            source=payload.source,
            timestamp=timestamp,
        )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_transaction(
            db, tenant, idempotency_key=payload.idempotency_key
        )
        if existing:
            if _existing_create_matches(existing, payload):
                return _response_for(
                    db,
                    tenant,
                    existing,
                    True,
                    include_acquisition_cost=user.get("role") == "admin",
                )
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for a different transaction.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    try:
        attempt_transaction_exchange(db, tenant, transaction_id)
    except Exception:
        logger.exception(
            "Unexpected exchange bookkeeping failure for %s", transaction_id
        )
    transaction = _load_transaction(db, tenant, transaction_id=transaction_id)
    if transaction is None:
        raise RuntimeError("Committed transaction could not be reloaded.")
    return _response_for(
        db,
        tenant,
        transaction,
        False,
        include_acquisition_cost=user.get("role") == "admin",
    )


def reverse_transaction(
    db: Any,
    user: dict[str, Any],
    original_transaction_id: str,
    payload: ReversalCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    existing = _load_transaction(db, tenant, idempotency_key=payload.idempotency_key)
    if existing:
        if not _existing_reversal_matches(existing, original_transaction_id, payload):
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for a different reversal.",
            )
        return _response_for(
            db,
            tenant,
            existing,
            True,
            include_acquisition_cost=user.get("role") == "admin",
        )

    reversal_id = str(uuid.uuid4())
    timestamp = utc_now()
    try:
        begin_write(db)
        existing = _load_transaction(
            db, tenant, idempotency_key=payload.idempotency_key
        )
        if existing:
            if not _existing_reversal_matches(
                existing, original_transaction_id, payload
            ):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for a different reversal.",
                )
            db.commit()
            return _response_for(
                db,
                tenant,
                existing,
                True,
                include_acquisition_cost=user.get("role") == "admin",
            )

        original = fetchone(
            db,
            "SELECT * FROM inventory_transactions WHERE tenant_id = ? AND id = ?",
            (tenant, original_transaction_id),
            "SELECT * FROM inventory_transactions WHERE tenant_id = %s AND id = %s FOR UPDATE",
        )
        if original is None:
            raise DomainError(404, "transaction_not_found", "Transaction not found.")
        if original["movement_type"] == "reversal":
            raise DomainError(409, "invalid_reversal", "A reversal cannot be reversed.")
        if original.get("reversed_by_transaction_id"):
            existing = _load_transaction(
                db, tenant, idempotency_key=payload.idempotency_key
            )
            if existing and _existing_reversal_matches(
                existing, original_transaction_id, payload
            ):
                db.commit()
                return _response_for(
                    db,
                    tenant,
                    existing,
                    True,
                    include_acquisition_cost=user.get("role") == "admin",
                )
            raise DomainError(
                409, "already_reversed", "This transaction was already reversed."
            )

        item = _item_by_sku_for_write(db, tenant, original["sku"])
        if item is None:
            raise DomainError(
                409, "missing_inventory", "The inventory item no longer exists."
            )
        location = _location_for_write(
            db,
            tenant,
            original["location_code"],
            require_active_stockable=False,
        )
        position = _position_for_write(
            db, tenant, item["sku"], original["location_code"], timestamp
        )
        _assert_position_total(db, tenant, item["sku"], item["current_stock"])
        quantity_delta = -original["quantity_delta"]
        if quantity_delta < 0:
            _require_available_stock(
                db,
                tenant,
                item,
                position,
                abs(quantity_delta),
            )
        new_location_stock = position["quantity"] + quantity_delta
        if new_location_stock < 0:
            raise DomainError(
                409,
                "negative_location_inventory",
                "This reversal would make stock at the original location negative. "
                "Correct later movements first.",
            )
        new_stock = item["current_stock"] + quantity_delta
        if new_stock < 0:
            raise DomainError(
                409,
                "negative_inventory",
                "This reversal would make inventory negative. Correct later movements first.",
            )

        _update_position_balance(db, tenant, position, new_location_stock, timestamp)
        _update_balance(db, tenant, item, new_stock, timestamp)
        _assert_position_total(db, tenant, item["sku"], new_stock)
        _insert_transaction(
            db,
            transaction_id=reversal_id,
            tenant=tenant,
            idempotency_key=payload.idempotency_key,
            item=item,
            movement_type="reversal",
            quantity=abs(quantity_delta),
            quantity_delta=quantity_delta,
            balance_before=item["current_stock"],
            balance_after=new_stock,
            location=location,
            location_balance_before=position["quantity"],
            location_balance_after=new_location_stock,
            user=user,
            reason=payload.reason,
            source="manual",
            timestamp=timestamp,
            reverses_transaction_id=original_transaction_id,
        )
        execute_write(
            db,
            "UPDATE inventory_transactions SET reversed_by_transaction_id = ? WHERE id = ?",
            (reversal_id, original_transaction_id),
            "UPDATE inventory_transactions SET reversed_by_transaction_id = %s WHERE id = %s",
        )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_transaction(
            db, tenant, idempotency_key=payload.idempotency_key
        )
        if existing:
            if _existing_reversal_matches(existing, original_transaction_id, payload):
                return _response_for(
                    db,
                    tenant,
                    existing,
                    True,
                    include_acquisition_cost=user.get("role") == "admin",
                )
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for a different reversal.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    try:
        attempt_transaction_exchange(db, tenant, reversal_id)
    except Exception:
        logger.exception("Unexpected exchange bookkeeping failure for %s", reversal_id)
    transaction = _load_transaction(db, tenant, transaction_id=reversal_id)
    if transaction is None:
        raise RuntimeError("Committed reversal could not be reloaded.")
    return _response_for(
        db,
        tenant,
        transaction,
        False,
        include_acquisition_cost=user.get("role") == "admin",
    )


def transaction_history(db: Any, tenant: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = fetchall(
        db,
        """
        SELECT t.*, i.product_name, i.category,
               o.status AS exchange_status, o.attempts AS exchange_attempts,
               o.last_error AS exchange_error, o.last_attempt_at, o.synced_at
        FROM inventory_transactions t
        JOIN inventory i ON i.tenant_id = t.tenant_id AND i.sku = t.sku
        JOIN sheets_outbox o ON o.transaction_id = t.id
        WHERE t.tenant_id = ?
        ORDER BY t.created_at DESC LIMIT ?
        """,
        (tenant, limit),
        """
        SELECT t.*, i.product_name, i.category,
               o.status AS exchange_status, o.attempts AS exchange_attempts,
               o.last_error AS exchange_error, o.last_attempt_at, o.synced_at
        FROM inventory_transactions t
        JOIN inventory i ON i.tenant_id = t.tenant_id AND i.sku = t.sku
        JOIN sheets_outbox o ON o.transaction_id = t.id
        WHERE t.tenant_id = %s
        ORDER BY t.created_at DESC LIMIT %s
        """,
    )
    return [transaction_public(row) for row in rows]


def _public_item_by_sku(
    db: Any,
    tenant: str,
    sku: str,
    *,
    include_acquisition_cost: bool = False,
) -> dict[str, Any] | None:
    row = fetchone(
        db,
        """
        SELECT sku, barcode, product_name, category, unit, variant,
               acquisition_cost_centavos, image_url,
               track_individually, active, current_stock, status,
               last_updated, version
        FROM inventory WHERE tenant_id = ? AND sku = ?
        """,
        (tenant, sku),
        """
        SELECT sku, barcode, product_name, category, unit, variant,
               acquisition_cost_centavos, image_url,
               track_individually, active, current_stock, status,
               last_updated, version
        FROM inventory WHERE tenant_id = %s AND sku = %s
        """,
    )
    if row is None:
        return None
    positions = _positions_by_sku(db, tenant)
    return inventory_public(
        row,
        positions.get(row["sku"], []),
        include_acquisition_cost=include_acquisition_cost,
    )


def _product_by_sku(db: Any, tenant: str, sku: str) -> dict[str, Any] | None:
    return fetchone(
        db,
        "SELECT * FROM inventory WHERE tenant_id = ? AND sku = ?",
        (tenant, sku),
        "SELECT * FROM inventory WHERE tenant_id = %s AND sku = %s",
    )


def _product_by_barcode(db: Any, tenant: str, barcode: str) -> dict[str, Any] | None:
    return fetchone(
        db,
        "SELECT * FROM inventory WHERE tenant_id = ? AND barcode = ?",
        (tenant, barcode),
        "SELECT * FROM inventory WHERE tenant_id = %s AND barcode = %s",
    )


def _insert_product_record(
    db: Any,
    tenant: str,
    product: ProductCreate | ProductReceiveCreate,
    timestamp: str,
) -> None:
    execute_write(
        db,
        """
        INSERT INTO inventory
            (tenant_id, sku, barcode, product_name, category, unit, variant,
             acquisition_cost_centavos, track_individually, active,
             current_stock, status, last_updated, version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'out_of_stock', ?, 0)
        """,
        (
            tenant,
            product.sku,
            product.barcode,
            product.product_name,
            product.category,
            product.unit,
            product.variant,
            product.acquisition_cost_centavos,
            product.track_individually,
            product.active,
            timestamp,
        ),
        """
        INSERT INTO inventory
            (tenant_id, sku, barcode, product_name, category, unit, variant,
             acquisition_cost_centavos, track_individually, active,
             current_stock, status, last_updated, version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 'out_of_stock', %s, 0)
        """,
    )


def _same_product_value(field: str, current: Any, requested: Any) -> bool:
    if field in {"track_individually", "active"}:
        return bool(current) == bool(requested)
    return current == requested


def _update_product_record(
    db: Any,
    tenant: str,
    item: dict[str, Any],
    requested: dict[str, Any],
    timestamp: str,
) -> tuple[dict[str, Any], bool]:
    allowed = {
        "barcode",
        "product_name",
        "category",
        "unit",
        "variant",
        "acquisition_cost_centavos",
        "image_url",
        "image_storage_key",
        "track_individually",
        "active",
    }
    changes = {
        field: value
        for field, value in requested.items()
        if field in allowed and not _same_product_value(field, item.get(field), value)
    }
    if not changes:
        return item, False

    sqlite_assignments = ", ".join(f"{field} = ?" for field in changes)
    postgres_assignments = ", ".join(f"{field} = %s" for field in changes)
    values = tuple(changes.values())
    rowcount = execute_write(
        db,
        f"""
        UPDATE inventory
        SET {sqlite_assignments}, last_updated = ?, version = version + 1
        WHERE tenant_id = ? AND sku = ? AND version = ?
        """,
        values
        + (
            timestamp,
            tenant,
            item["sku"],
            item.get("version", 0),
        ),
        f"""
        UPDATE inventory
        SET {postgres_assignments}, last_updated = %s, version = version + 1
        WHERE tenant_id = %s AND sku = %s AND version = %s
        """,
    )
    if rowcount != 1:
        raise DomainError(
            409,
            "product_conflict",
            "This product changed while it was being saved. Review and retry.",
        )
    refreshed = _item_by_sku_for_write(db, tenant, item["sku"])
    if refreshed is None:
        raise RuntimeError("Updated product could not be reloaded.")
    return refreshed, True


def _apply_stock_delta(
    db: Any,
    *,
    tenant: str,
    user: dict[str, Any],
    item: dict[str, Any],
    location: dict[str, Any],
    quantity_delta: int,
    idempotency_key: str,
    reason: str,
    source: str,
    timestamp: str,
    exchange_status: str = "pending",
    availability_allowance: int = 0,
) -> str:
    if quantity_delta == 0:
        raise ValueError("A zero stock change cannot create a transaction.")

    position = _position_for_write(db, tenant, item["sku"], location["code"], timestamp)
    _assert_position_total(db, tenant, item["sku"], item["current_stock"])
    if quantity_delta < 0:
        _require_available_stock(
            db,
            tenant,
            item,
            position,
            abs(quantity_delta),
            allowance=availability_allowance,
        )
    new_location_stock = position["quantity"] + quantity_delta
    if new_location_stock < 0:
        raise DomainError(
            409,
            "negative_location_inventory",
            f"Only {position['quantity']} units are available at {location['name']}.",
        )
    new_stock = item["current_stock"] + quantity_delta
    if new_stock < 0:
        raise DomainError(
            409,
            "negative_inventory",
            f"Only {item['current_stock']} units are available. Stock cannot become negative.",
        )

    _update_position_balance(db, tenant, position, new_location_stock, timestamp)
    _update_balance(db, tenant, item, new_stock, timestamp)
    _assert_position_total(db, tenant, item["sku"], new_stock)
    transaction_id = str(uuid.uuid4())
    _insert_transaction(
        db,
        transaction_id=transaction_id,
        tenant=tenant,
        idempotency_key=idempotency_key,
        item=item,
        movement_type="stock_in" if quantity_delta > 0 else "stock_out",
        quantity=abs(quantity_delta),
        quantity_delta=quantity_delta,
        balance_before=item["current_stock"],
        balance_after=new_stock,
        location=location,
        location_balance_before=position["quantity"],
        location_balance_after=new_location_stock,
        user=user,
        reason=reason,
        source=source,
        timestamp=timestamp,
        exchange_status=exchange_status,
    )
    return transaction_id


def _raise_product_identity_conflict(
    db: Any, tenant: str, sku: str, barcode: str
) -> None:
    if _product_by_sku(db, tenant, sku) is not None:
        raise DomainError(409, "sku_exists", "A product already uses this SKU.")
    if _product_by_barcode(db, tenant, barcode) is not None:
        raise DomainError(409, "barcode_exists", "A product already uses this barcode.")


def create_product_with_stock(
    db: Any,
    user: dict[str, Any],
    payload: ProductReceiveCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    existing = _load_transaction(db, tenant, idempotency_key=payload.idempotency_key)
    if existing:
        if not _existing_product_create_matches(existing, payload):
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another action.",
            )
        return _response_for(
            db,
            tenant,
            existing,
            True,
            include_acquisition_cost=user.get("role") == "admin",
        )

    if not payload.active:
        raise DomainError(
            422,
            "inactive_product",
            "A new product with opening stock must be active.",
        )

    timestamp = utc_now()
    transaction_id: str | None = None
    reason = payload.reason or "Opening stock"
    try:
        begin_write(db)
        existing = _load_transaction(
            db, tenant, idempotency_key=payload.idempotency_key
        )
        if existing:
            if not _existing_product_create_matches(existing, payload):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another action.",
                )
            db.commit()
            return _response_for(
                db,
                tenant,
                existing,
                True,
                include_acquisition_cost=user.get("role") == "admin",
            )

        _raise_product_identity_conflict(db, tenant, payload.sku, payload.barcode)
        location = _location_for_write(db, tenant, payload.location_code)
        _insert_product_record(db, tenant, payload, timestamp)
        item = _item_by_sku_for_write(db, tenant, payload.sku)
        if item is None:
            raise RuntimeError("Created product could not be loaded.")
        transaction_id = _apply_stock_delta(
            db,
            tenant=tenant,
            user=user,
            item=item,
            location=location,
            quantity_delta=payload.quantity,
            idempotency_key=payload.idempotency_key,
            reason=reason,
            source="manual",
            timestamp=timestamp,
        )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_transaction(
            db, tenant, idempotency_key=payload.idempotency_key
        )
        if existing:
            if _existing_product_create_matches(existing, payload):
                return _response_for(
                    db,
                    tenant,
                    existing,
                    True,
                    include_acquisition_cost=user.get("role") == "admin",
                )
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another action.",
            )
        _raise_product_identity_conflict(db, tenant, payload.sku, payload.barcode)
        raise
    except Exception:
        db.rollback()
        raise

    if transaction_id is None:
        raise RuntimeError("Product receipt did not create a transaction.")
    try:
        attempt_transaction_exchange(db, tenant, transaction_id)
    except Exception:
        logger.exception(
            "Unexpected exchange bookkeeping failure for %s", transaction_id
        )
    transaction = _load_transaction(db, tenant, transaction_id=transaction_id)
    if transaction is None:
        raise RuntimeError("Committed product receipt could not be reloaded.")
    return _response_for(
        db,
        tenant,
        transaction,
        False,
        include_acquisition_cost=user.get("role") == "admin",
    )


def update_product(
    db: Any,
    user: dict[str, Any],
    sku: str,
    payload: ProductUpdate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_sku = sku.strip().upper()
    requested = payload.model_dump(exclude_unset=True)
    timestamp = utc_now()
    try:
        begin_write(db)
        item = _item_by_sku_for_write(db, tenant, normalized_sku)
        if item is None:
            raise DomainError(404, "product_not_found", "Product not found.")

        requested_barcode = requested.get("barcode")
        if requested_barcode and requested_barcode != item["barcode"]:
            owner = _item_for_write(db, tenant, requested_barcode)
            if owner is not None and owner["sku"] != normalized_sku:
                raise DomainError(
                    409,
                    "barcode_exists",
                    "A product already uses this barcode.",
                )

        _update_product_record(db, tenant, item, requested, timestamp)
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        requested_barcode = requested.get("barcode")
        if requested_barcode:
            owner = _product_by_barcode(db, tenant, requested_barcode)
            if owner is not None and owner["sku"] != normalized_sku:
                raise DomainError(
                    409,
                    "barcode_exists",
                    "A product already uses this barcode.",
                )
        raise
    except Exception:
        db.rollback()
        raise

    result = _public_item_by_sku(
        db,
        tenant,
        normalized_sku,
        include_acquisition_cost=user.get("role") == "admin",
    )
    if result is None:
        raise RuntimeError("Updated product could not be reloaded.")
    return result


def replace_product_image_metadata(
    db: Any,
    user: dict[str, Any],
    sku: str,
    *,
    image_url: str | None,
    image_storage_key: str | None,
) -> tuple[dict[str, Any], str | None]:
    if user.get("role") != "admin":
        raise DomainError(
            403,
            "admin_required",
            "Administrator access is required.",
        )
    if (image_url is None) != (image_storage_key is None):
        raise DomainError(
            422,
            "invalid_product_image",
            "Image URL and storage key must be set or cleared together.",
        )

    normalized_url: str | None = None
    normalized_key: str | None = None
    if image_url is not None and image_storage_key is not None:
        normalized_url = image_url.strip()
        normalized_key = image_storage_key.strip()
        if not normalized_url or not normalized_key:
            raise DomainError(
                422,
                "invalid_product_image",
                "Image URL and storage key cannot be blank.",
            )
        if len(normalized_url) > 2_048 or len(normalized_key) > 512:
            raise DomainError(
                422,
                "invalid_product_image",
                "Product image metadata is too long.",
            )

    tenant = user["tenant_id"]
    normalized_sku = sku.strip().upper()
    previous_storage_key: str | None = None
    try:
        begin_write(db)
        item = _item_by_sku_for_write(db, tenant, normalized_sku)
        if item is None:
            raise DomainError(404, "product_not_found", "Product not found.")
        previous_storage_key = item.get("image_storage_key")
        _update_product_record(
            db,
            tenant,
            item,
            {
                "image_url": normalized_url,
                "image_storage_key": normalized_key,
            },
            utc_now(),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    result = _public_item_by_sku(
        db,
        tenant,
        normalized_sku,
        include_acquisition_cost=True,
    )
    if result is None:
        raise RuntimeError("Updated product could not be reloaded.")
    cleanup_key = (
        previous_storage_key
        if previous_storage_key and previous_storage_key != normalized_key
        else None
    )
    return result, cleanup_key


def _normalize_csv_header(value: str) -> str:
    return value.lstrip("\ufeff").strip().lower().replace("-", "_").replace(" ", "_")


def _csv_boolean(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError("use true or false")


def _csv_optional_centavos(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().replace(",", "")
    if not normalized or any(character not in "0123456789" for character in normalized):
        raise ValueError("acquisition_cost_centavos must be a whole number")
    centavos = int(normalized)
    if centavos > 1_000_000_000:
        raise ValueError("acquisition_cost_centavos cannot exceed 1,000,000,000")
    return centavos


def _validation_message(error: ValidationError) -> str:
    first = error.errors()[0]
    field = ".".join(str(part) for part in first.get("loc", ()))
    message = first.get("msg", "invalid value")
    return f"{field}: {message}" if field else str(message)


def _parse_import_csv(
    csv_text: str,
) -> tuple[list[ImportRow], list[str], int]:
    reader = csv.DictReader(io.StringIO(csv_text, newline=""))
    if reader.fieldnames is None:
        raise DomainError(422, "invalid_csv", "The CSV header row is missing.")

    headers = [_normalize_csv_header(header or "") for header in reader.fieldnames]
    if any(not header for header in headers) or len(headers) != len(set(headers)):
        raise DomainError(
            422,
            "invalid_csv_headers",
            "CSV column names must be present and unique.",
        )
    reader.fieldnames = headers
    required = {
        "sku",
        "barcode",
        "product_name",
        "category",
        "unit",
        "location_code",
        "quantity",
    }
    missing = sorted(required.difference(headers))
    if missing:
        raise DomainError(
            422,
            "missing_csv_columns",
            f"Missing CSV columns: {', '.join(missing)}.",
        )

    provided_fields = frozenset(headers)
    parsed: list[ImportRow] = []
    errors: list[str] = []
    skipped = 0
    seen_positions: set[tuple[str, str]] = set()
    product_signatures: dict[str, tuple[Any, ...]] = {}
    barcode_owners: dict[str, str] = {}
    nonempty_rows = 0

    for line_number, raw_row in enumerate(reader, start=2):
        values = {
            key: (value.strip() if isinstance(value, str) else value)
            for key, value in raw_row.items()
            if key is not None
        }
        if not any(value for value in values.values()):
            continue
        nonempty_rows += 1
        if nonempty_rows > 5_000:
            raise DomainError(
                422,
                "too_many_csv_rows",
                "Import at most 5,000 rows at a time.",
            )
        if None in raw_row:
            errors.append(f"Row {line_number}: too many columns.")
            skipped += 1
            continue

        try:
            raw_quantity = str(values.get("quantity") or "").replace(",", "")
            if not raw_quantity or any(
                character not in "0123456789" for character in raw_quantity
            ):
                raise ValueError("quantity must be a whole number")
            quantity = int(raw_quantity)
            if quantity > 1_000_000:
                raise ValueError("quantity cannot exceed 1,000,000")

            product = ProductCreate(
                sku=str(values.get("sku") or ""),
                barcode=str(values.get("barcode") or ""),
                product_name=str(values.get("product_name") or ""),
                category=str(values.get("category") or ""),
                unit=str(values.get("unit") or ""),
                variant=values.get("variant") or None,
                acquisition_cost_centavos=_csv_optional_centavos(
                    values.get("acquisition_cost_centavos")
                ),
                track_individually=_csv_boolean(
                    values.get("track_individually"), False
                ),
                active=_csv_boolean(values.get("active"), True),
            )
            location_code = str(values.get("location_code") or "").strip().upper()
            if not location_code or len(location_code) > 64:
                raise ValueError("location_code is invalid")
            if quantity > 0 and not product.active:
                raise ValueError("an inactive product cannot receive stock")
        except ValidationError as error:
            errors.append(f"Row {line_number}: {_validation_message(error)}.")
            skipped += 1
            continue
        except ValueError as error:
            errors.append(f"Row {line_number}: {error}.")
            skipped += 1
            continue

        position_key = (product.sku, location_code)
        if position_key in seen_positions:
            errors.append(
                f"Row {line_number}: duplicate SKU and location in this file."
            )
            skipped += 1
            continue

        signature = (
            product.barcode,
            product.product_name,
            product.category,
            product.unit,
            product.variant,
            product.acquisition_cost_centavos,
            product.track_individually,
            product.active,
        )
        previous_signature = product_signatures.get(product.sku)
        if previous_signature is not None and previous_signature != signature:
            errors.append(
                f"Row {line_number}: product details conflict with another row for this SKU."
            )
            skipped += 1
            continue
        previous_sku = barcode_owners.get(product.barcode)
        if previous_sku is not None and previous_sku != product.sku:
            errors.append(
                f"Row {line_number}: barcode is assigned to another SKU in this file."
            )
            skipped += 1
            continue

        seen_positions.add(position_key)
        product_signatures[product.sku] = signature
        barcode_owners[product.barcode] = product.sku
        parsed.append(
            ImportRow(
                line_number=line_number,
                product=product,
                location_code=location_code,
                quantity=quantity,
                reason=(str(values.get("reason") or "").strip() or None),
                provided_fields=provided_fields,
            )
        )

    if nonempty_rows == 0:
        raise DomainError(422, "empty_csv", "The CSV contains no product rows.")
    return parsed, errors, skipped


def _load_import_record(
    db: Any, tenant: str, idempotency_key: str
) -> dict[str, Any] | None:
    return fetchone(
        db,
        """
        SELECT * FROM inventory_imports
        WHERE tenant_id = ? AND idempotency_key = ?
        """,
        (tenant, idempotency_key),
        """
        SELECT * FROM inventory_imports
        WHERE tenant_id = %s AND idempotency_key = %s
        """,
    )


def _import_public(record: dict[str, Any], replayed: bool) -> dict[str, Any]:
    try:
        errors = json.loads(record.get("errors_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        errors = ["Stored import errors could not be read."]
    return {
        "created": int(record.get("created", 0)),
        "updated": int(record.get("updated", 0)),
        "skipped": int(record.get("skipped", 0)),
        "errors": errors,
        "replayed": replayed,
    }


def _matching_import_or_error(
    record: dict[str, Any], content_hash: str
) -> dict[str, Any]:
    if record["content_hash"] != content_hash:
        raise DomainError(
            409,
            "idempotency_conflict",
            "This import key was already used for a different file.",
        )
    return _import_public(record, True)


def _import_row_key(batch_key: str, row: ImportRow) -> str:
    material = (
        f"{batch_key}|{row.line_number}|{row.product.sku}|" f"{row.location_code}"
    )
    return "import-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def import_inventory_csv(
    db: Any,
    user: dict[str, Any],
    csv_text: str | bytes,
    idempotency_key: str,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_key = idempotency_key.strip()
    if len(normalized_key) < 8 or len(normalized_key) > 128:
        raise DomainError(
            422,
            "invalid_idempotency_key",
            "Import key must be between 8 and 128 characters.",
        )
    if isinstance(csv_text, bytes):
        try:
            decoded = csv_text.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise DomainError(
                422, "invalid_csv_encoding", "Upload a UTF-8 CSV file."
            ) from error
    else:
        decoded = csv_text.lstrip("\ufeff")

    content_hash = hashlib.sha256(decoded.encode("utf-8")).hexdigest()
    existing = _load_import_record(db, tenant, normalized_key)
    if existing:
        return _matching_import_or_error(existing, content_hash)

    rows, errors, skipped = _parse_import_csv(decoded)
    created = 0
    updated = 0
    try:
        begin_write(db)
        existing = _load_import_record(db, tenant, normalized_key)
        if existing:
            db.commit()
            return _matching_import_or_error(existing, content_hash)

        for row in rows:
            try:
                location = _location_for_write(db, tenant, row.location_code)
            except DomainError as error:
                errors.append(f"Row {row.line_number}: {error.detail}")
                skipped += 1
                continue

            item = _item_by_sku_for_write(db, tenant, row.product.sku)
            barcode_owner = _item_for_write(db, tenant, row.product.barcode)
            if item is None and barcode_owner is not None:
                errors.append(
                    f"Row {row.line_number}: barcode belongs to SKU "
                    f"{barcode_owner['sku']}."
                )
                skipped += 1
                continue
            if (
                item is not None
                and barcode_owner is not None
                and barcode_owner["sku"] != item["sku"]
            ):
                errors.append(
                    f"Row {row.line_number}: barcode belongs to SKU "
                    f"{barcode_owner['sku']}."
                )
                skipped += 1
                continue

            timestamp = utc_now()
            new_product = item is None
            if new_product:
                _insert_product_record(db, tenant, row.product, timestamp)
                item = _item_by_sku_for_write(db, tenant, row.product.sku)
                if item is None:
                    raise RuntimeError("Imported product could not be loaded.")
                created += 1
                metadata_changed = False
            else:
                requested: dict[str, Any] = {
                    "barcode": row.product.barcode,
                    "product_name": row.product.product_name,
                    "category": row.product.category,
                    "unit": row.product.unit,
                }
                for optional_field in (
                    "variant",
                    "acquisition_cost_centavos",
                    "track_individually",
                    "active",
                ):
                    if (
                        optional_field in row.provided_fields
                        and (
                            optional_field != "acquisition_cost_centavos"
                            or row.product.acquisition_cost_centavos is not None
                        )
                    ):
                        requested[optional_field] = getattr(row.product, optional_field)
                item, metadata_changed = _update_product_record(
                    db, tenant, item, requested, timestamp
                )

            position = _position_for_write(
                db,
                tenant,
                item["sku"],
                row.location_code,
                timestamp,
            )
            _assert_position_total(db, tenant, item["sku"], item["current_stock"])
            quantity_delta = row.quantity - position["quantity"]
            stock_changed = quantity_delta != 0
            if stock_changed:
                if not bool(item.get("active", True)):
                    raise DomainError(
                        409,
                        "inactive_product",
                        f"Row {row.line_number}: reactivate the product before importing stock.",
                    )
                _apply_stock_delta(
                    db,
                    tenant=tenant,
                    user=user,
                    item=item,
                    location=location,
                    quantity_delta=quantity_delta,
                    idempotency_key=_import_row_key(normalized_key, row),
                    reason=row.reason or "Imported opening balance",
                    source="import",
                    timestamp=timestamp,
                    exchange_status="not_configured",
                )

            if not new_product:
                if metadata_changed or stock_changed:
                    updated += 1
                else:
                    skipped += 1

        result_record = {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors_json": json.dumps(errors, ensure_ascii=False),
        }
        execute_write(
            db,
            """
            INSERT INTO inventory_imports
                (id, tenant_id, idempotency_key, content_hash, created,
                 updated, skipped, errors_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                tenant,
                normalized_key,
                content_hash,
                created,
                updated,
                skipped,
                result_record["errors_json"],
                utc_now(),
            ),
            """
            INSERT INTO inventory_imports
                (id, tenant_id, idempotency_key, content_hash, created,
                 updated, skipped, errors_json, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
        )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_import_record(db, tenant, normalized_key)
        if existing:
            return _matching_import_or_error(existing, content_hash)
        raise
    except Exception:
        db.rollback()
        raise

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "replayed": False,
    }


def _load_transfer(
    db: Any,
    tenant: str,
    *,
    transfer_id: str | None = None,
    create_idempotency_key: str | None = None,
    receive_idempotency_key: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    provided = sum(
        value is not None
        for value in (
            transfer_id,
            create_idempotency_key,
            receive_idempotency_key,
        )
    )
    if provided != 1:
        raise ValueError("Load a transfer by exactly one identifier.")

    if transfer_id is not None:
        sqlite_where = "tenant_id = ? AND id = ?"
        postgres_where = "tenant_id = %s AND id = %s"
        params = (tenant, transfer_id)
    elif create_idempotency_key is not None:
        sqlite_where = "tenant_id = ? AND create_idempotency_key = ?"
        postgres_where = "tenant_id = %s AND create_idempotency_key = %s"
        params = (tenant, create_idempotency_key)
    else:
        sqlite_where = "tenant_id = ? AND receive_idempotency_key = ?"
        postgres_where = "tenant_id = %s AND receive_idempotency_key = %s"
        params = (tenant, receive_idempotency_key)

    transfer = fetchone(
        db,
        f"SELECT * FROM inventory_transfers WHERE {sqlite_where}",
        params,
        (
            f"SELECT * FROM inventory_transfers WHERE {postgres_where}"
            + (" FOR UPDATE" if for_update else "")
        ),
    )
    if transfer is None:
        return None

    transfer["lines"] = fetchall(
        db,
        """
        SELECT sku, barcode, product_name, quantity_sent, quantity_received,
               discrepancy_note, source_balance_before, source_balance_after,
               destination_balance_before, destination_balance_after
        FROM inventory_transfer_lines
        WHERE tenant_id = ? AND transfer_id = ?
        ORDER BY product_name, sku
        """,
        (tenant, transfer["id"]),
        """
        SELECT sku, barcode, product_name, quantity_sent, quantity_received,
               discrepancy_note, source_balance_before, source_balance_after,
               destination_balance_before, destination_balance_after
        FROM inventory_transfer_lines
        WHERE tenant_id = %s AND transfer_id = %s
        ORDER BY product_name, sku
        """,
    )
    return transfer


def transfer_public(transfer: dict[str, Any]) -> dict[str, Any]:
    lines = [
        {
            "sku": line["sku"],
            "barcode": line["barcode"],
            "product_name": line["product_name"],
            "quantity_sent": int(line["quantity_sent"]),
            "quantity_received": (
                int(line["quantity_received"])
                if line.get("quantity_received") is not None
                else None
            ),
            "discrepancy_note": line.get("discrepancy_note"),
            "source_balance_before": line.get("source_balance_before"),
            "source_balance_after": line.get("source_balance_after"),
            "destination_balance_before": line.get(
                "destination_balance_before"
            ),
            "destination_balance_after": line.get(
                "destination_balance_after"
            ),
        }
        for line in transfer.get("lines", [])
    ]
    return {
        "id": transfer["id"],
        "status": transfer["status"],
        "source_location_code": transfer["source_location_code"],
        "source_location_name": transfer["source_location_name"],
        "source_location_path": transfer["source_location_path"],
        "destination_location_code": transfer["destination_location_code"],
        "destination_location_name": transfer["destination_location_name"],
        "destination_location_path": transfer["destination_location_path"],
        "initiated_by_user_id": transfer["initiated_by_user_id"],
        "initiated_by_name": transfer["initiated_by_name"],
        "received_by_user_id": transfer.get("received_by_user_id"),
        "received_by_name": transfer.get("received_by_name"),
        "note": transfer.get("note"),
        "receive_note": transfer.get("receive_note"),
        "created_at": transfer["created_at"],
        "received_at": transfer.get("received_at"),
        "has_discrepancy": (
            transfer["status"] == "received_with_discrepancy"
            or any(
                line["quantity_received"] is not None
                and (
                    line["quantity_received"] != line["quantity_sent"]
                    or bool(line["discrepancy_note"])
                )
                for line in lines
            )
        ),
        "lines": lines,
    }


def _transfer_response(
    transfer: dict[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "authority": "Stockpile application database",
        "data": transfer_public(transfer),
        "replayed": replayed,
    }


def list_transfers(
    db: Any,
    tenant: str,
    limit: int = 100,
    status: str | None = None,
) -> list[dict[str, Any]]:
    allowed_statuses = {
        "pending",
        "received",
        "received_with_discrepancy",
        "cancelled",
    }
    if status is not None and status not in allowed_statuses:
        raise DomainError(422, "invalid_transfer_status", "Transfer status is invalid.")

    if status is None:
        rows = fetchall(
            db,
            """
            SELECT id FROM inventory_transfers
            WHERE tenant_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (tenant, limit),
            """
            SELECT id FROM inventory_transfers
            WHERE tenant_id = %s
            ORDER BY created_at DESC LIMIT %s
            """,
        )
    else:
        rows = fetchall(
            db,
            """
            SELECT id FROM inventory_transfers
            WHERE tenant_id = ? AND status = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (tenant, status, limit),
            """
            SELECT id FROM inventory_transfers
            WHERE tenant_id = %s AND status = %s
            ORDER BY created_at DESC LIMIT %s
            """,
        )

    result: list[dict[str, Any]] = []
    for row in rows:
        transfer = _load_transfer(db, tenant, transfer_id=row["id"])
        if transfer is not None:
            result.append(transfer_public(transfer))
    return result


def _existing_transfer_create_matches(
    transfer: dict[str, Any],
    payload: TransferCreate,
) -> bool:
    existing_lines = {
        line["barcode"]: int(line["quantity_sent"])
        for line in transfer.get("lines", [])
    }
    requested_lines = {
        line.barcode: line.quantity
        for line in payload.lines
    }
    return (
        transfer["source_location_code"] == payload.source_location_code
        and transfer["destination_location_code"]
        == payload.destination_location_code
        and (transfer.get("note") or None) == payload.note
        and existing_lines == requested_lines
    )


def _receipt_expectations(
    transfer: dict[str, Any],
    payload: TransferReceive,
) -> dict[str, tuple[int, str | None]]:
    transfer_barcodes = {
        line["barcode"]
        for line in transfer.get("lines", [])
    }
    supplied = {
        discrepancy.barcode: (
            discrepancy.quantity_received,
            discrepancy.note,
        )
        for discrepancy in payload.discrepancies
    }
    unknown = sorted(set(supplied).difference(transfer_barcodes))
    if unknown:
        raise DomainError(
            422,
            "transfer_line_not_found",
            "A received item is not part of this transfer.",
        )

    return {
        line["barcode"]: supplied.get(
            line["barcode"],
            (int(line["quantity_sent"]), None),
        )
        for line in transfer.get("lines", [])
    }


def _existing_transfer_receive_matches(
    transfer: dict[str, Any],
    payload: TransferReceive,
    receiver_user_id: str,
) -> bool:
    if transfer["status"] not in {
        "received",
        "received_with_discrepancy",
    }:
        return False
    if (transfer.get("receive_note") or None) != payload.note:
        return False
    if transfer.get("received_by_user_id") != receiver_user_id:
        return False
    try:
        expected = _receipt_expectations(transfer, payload)
    except DomainError:
        return False

    for line in transfer.get("lines", []):
        quantity, note = expected[line["barcode"]]
        if line.get("quantity_received") is None:
            return False
        if int(line["quantity_received"]) != quantity:
            return False
        if (line.get("discrepancy_note") or None) != note:
            return False
    return True


def create_transfer(
    db: Any,
    user: dict[str, Any],
    payload: TransferCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    existing = _load_transfer(
        db,
        tenant,
        create_idempotency_key=payload.idempotency_key,
    )
    if existing:
        if not _existing_transfer_create_matches(existing, payload):
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another transfer.",
            )
        return _transfer_response(existing, replayed=True)

    transfer_id = str(uuid.uuid4())
    timestamp = utc_now()
    try:
        begin_write(db)
        existing = _load_transfer(
            db,
            tenant,
            create_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing:
            if not _existing_transfer_create_matches(existing, payload):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another transfer.",
                )
            db.commit()
            return _transfer_response(existing, replayed=True)

        source = _location_for_write(
            db,
            tenant,
            payload.source_location_code,
        )
        destination = _location_for_write(
            db,
            tenant,
            payload.destination_location_code,
        )
        if source["code"] == destination["code"]:
            raise DomainError(
                422,
                "same_transfer_location",
                "Source and destination locations must be different.",
            )

        prepared_lines: list[dict[str, Any]] = []
        for line in sorted(payload.lines, key=lambda entry: entry.barcode):
            item = _item_for_write(db, tenant, line.barcode)
            if item is None:
                raise DomainError(
                    404,
                    "unknown_barcode",
                    f"No product matches barcode {line.barcode}.",
                )
            if not bool(item.get("active", True)):
                raise DomainError(
                    409,
                    "inactive_product",
                    f"{item['product_name']} is archived.",
                )
            position = _position_for_write(
                db,
                tenant,
                item["sku"],
                source["code"],
                timestamp,
            )
            _assert_position_total(
                db,
                tenant,
                item["sku"],
                item["current_stock"],
            )
            _require_available_stock(
                db,
                tenant,
                item,
                position,
                line.quantity,
            )
            if int(position["quantity"]) < line.quantity:
                raise DomainError(
                    409,
                    "insufficient_source_stock",
                    f"Only {position['quantity']} units of "
                    f"{item['product_name']} are at {source['name']}.",
                )
            prepared_lines.append(
                {
                    "sku": item["sku"],
                    "barcode": item["barcode"],
                    "product_name": item["product_name"],
                    "quantity_sent": line.quantity,
                }
            )

        execute_write(
            db,
            """
            INSERT INTO inventory_transfers
                (id, tenant_id, create_idempotency_key, status,
                 source_location_code, source_location_name,
                 source_location_path, destination_location_code,
                 destination_location_name, destination_location_path,
                 initiated_by_user_id, initiated_by_name, note, created_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transfer_id,
                tenant,
                payload.idempotency_key,
                source["code"],
                source["name"],
                source["path"],
                destination["code"],
                destination["name"],
                destination["path"],
                user["id"],
                user["display_name"],
                payload.note,
                timestamp,
            ),
            """
            INSERT INTO inventory_transfers
                (id, tenant_id, create_idempotency_key, status,
                 source_location_code, source_location_name,
                 source_location_path, destination_location_code,
                 destination_location_name, destination_location_path,
                 initiated_by_user_id, initiated_by_name, note, created_at)
            VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
        )
        for line in prepared_lines:
            execute_write(
                db,
                """
                INSERT INTO inventory_transfer_lines
                    (tenant_id, transfer_id, sku, barcode, product_name,
                     quantity_sent)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant,
                    transfer_id,
                    line["sku"],
                    line["barcode"],
                    line["product_name"],
                    line["quantity_sent"],
                ),
                """
                INSERT INTO inventory_transfer_lines
                    (tenant_id, transfer_id, sku, barcode, product_name,
                     quantity_sent)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_transfer(
            db,
            tenant,
            create_idempotency_key=payload.idempotency_key,
        )
        if existing:
            if _existing_transfer_create_matches(existing, payload):
                return _transfer_response(existing, replayed=True)
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another transfer.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    transfer = _load_transfer(db, tenant, transfer_id=transfer_id)
    if transfer is None:
        raise RuntimeError("Committed transfer could not be reloaded.")
    return _transfer_response(transfer, replayed=False)


def _employee_for_badge(
    db: Any,
    tenant: str,
    badge_code: str,
    pin: str | None,
    *,
    for_update: bool = False,
    badge_error_code: str = "invalid_employee_badge",
    pin_required_code: str = "employee_pin_required",
    pin_error_code: str = "invalid_employee_pin",
) -> dict[str, Any]:
    digest = badge_code_digest(badge_code, tenant)
    employee = fetchone(
        db,
        """
        SELECT id, tenant_id, display_name, role, badge_pin_hash, active
        FROM users
        WHERE tenant_id = ? AND badge_code_digest = ? AND active = 1
        """,
        (tenant, digest),
        (
            """
            SELECT id, tenant_id, display_name, role, badge_pin_hash, active
            FROM users
            WHERE tenant_id = %s AND badge_code_digest = %s AND active = TRUE
            """
            + (" FOR UPDATE" if for_update else "")
        ),
    )
    if employee is None:
        raise DomainError(401, badge_error_code, "Badge not recognized.")

    pin_hash = employee.get("badge_pin_hash")
    if pin_hash:
        if not pin:
            raise DomainError(401, pin_required_code, "Enter the badge PIN.")
        try:
            valid_pin = verify_password(pin, pin_hash)
        except (TypeError, ValueError):
            valid_pin = False
        if not valid_pin:
            raise DomainError(401, pin_error_code, "Badge PIN is incorrect.")
    return employee


def _receiver_for_badge(
    db: Any,
    tenant: str,
    payload: TransferReceive,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    return _employee_for_badge(
        db,
        tenant,
        payload.receiver_badge_code,
        payload.receiver_pin,
        for_update=for_update,
        badge_error_code="invalid_receiver_badge",
        pin_required_code="receiver_pin_required",
        pin_error_code="invalid_receiver_pin",
    )


def receive_transfer(
    db: Any,
    user: dict[str, Any],
    transfer_id: str,
    payload: TransferReceive,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_id = transfer_id.strip()
    timestamp = utc_now()
    receiver: dict[str, Any] | None = None
    try:
        begin_write(db)
        receiver = _receiver_for_badge(
            db,
            tenant,
            payload,
            for_update=True,
        )
        existing_by_key = _load_transfer(
            db,
            tenant,
            receive_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing_by_key:
            if (
                existing_by_key["id"] != normalized_id
                or not _existing_transfer_receive_matches(
                    existing_by_key,
                    payload,
                    receiver["id"],
                )
            ):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This confirmation key was already used for another receipt.",
                )
            db.commit()
            return _transfer_response(existing_by_key, replayed=True)

        transfer = _load_transfer(
            db,
            tenant,
            transfer_id=normalized_id,
            for_update=True,
        )
        if transfer is None:
            raise DomainError(404, "transfer_not_found", "Transfer not found.")
        if transfer["status"] != "pending":
            raise DomainError(
                409,
                "transfer_already_closed",
                "This transfer has already been received or closed.",
            )

        _location_for_write(
            db,
            tenant,
            transfer["source_location_code"],
            require_active_stockable=False,
        )
        _location_for_write(
            db,
            tenant,
            transfer["destination_location_code"],
        )
        expectations = _receipt_expectations(transfer, payload)
        has_discrepancy = False

        for line in sorted(transfer["lines"], key=lambda entry: entry["sku"]):
            item = _item_by_sku_for_write(db, tenant, line["sku"])
            if item is None:
                raise DomainError(
                    409,
                    "missing_inventory",
                    f"{line['product_name']} no longer exists.",
                )
            if not bool(item.get("active", True)):
                raise DomainError(
                    409,
                    "inactive_product",
                    f"{item['product_name']} is archived.",
                )

            positions: dict[str, dict[str, Any]] = {}
            for location_code in sorted(
                {
                    transfer["source_location_code"],
                    transfer["destination_location_code"],
                }
            ):
                positions[location_code] = _position_for_write(
                    db,
                    tenant,
                    item["sku"],
                    location_code,
                    timestamp,
                )
            source_position = positions[transfer["source_location_code"]]
            destination_position = positions[
                transfer["destination_location_code"]
            ]
            _assert_position_total(
                db,
                tenant,
                item["sku"],
                item["current_stock"],
            )

            quantity_received, discrepancy_note = expectations[line["barcode"]]
            source_before = int(source_position["quantity"])
            destination_before = int(destination_position["quantity"])
            if quantity_received:
                _require_available_stock(
                    db,
                    tenant,
                    item,
                    source_position,
                    quantity_received,
                )
            if source_before < quantity_received:
                raise DomainError(
                    409,
                    "insufficient_source_stock",
                    f"Only {source_before} units of {item['product_name']} "
                    f"remain at {transfer['source_location_name']}.",
                )
            source_after = source_before - quantity_received
            destination_after = destination_before + quantity_received

            if quantity_received:
                _update_position_balance(
                    db,
                    tenant,
                    source_position,
                    source_after,
                    timestamp,
                )
                _update_position_balance(
                    db,
                    tenant,
                    destination_position,
                    destination_after,
                    timestamp,
                )
                _update_balance(
                    db,
                    tenant,
                    item,
                    item["current_stock"],
                    timestamp,
                )
            _assert_position_total(
                db,
                tenant,
                item["sku"],
                item["current_stock"],
            )

            line_has_discrepancy = (
                quantity_received != int(line["quantity_sent"])
                or bool(discrepancy_note)
            )
            has_discrepancy = has_discrepancy or line_has_discrepancy
            execute_write(
                db,
                """
                UPDATE inventory_transfer_lines
                SET quantity_received = ?, discrepancy_note = ?,
                    source_balance_before = ?, source_balance_after = ?,
                    destination_balance_before = ?,
                    destination_balance_after = ?
                WHERE tenant_id = ? AND transfer_id = ? AND sku = ?
                """,
                (
                    quantity_received,
                    discrepancy_note,
                    source_before,
                    source_after,
                    destination_before,
                    destination_after,
                    tenant,
                    normalized_id,
                    line["sku"],
                ),
                """
                UPDATE inventory_transfer_lines
                SET quantity_received = %s, discrepancy_note = %s,
                    source_balance_before = %s, source_balance_after = %s,
                    destination_balance_before = %s,
                    destination_balance_after = %s
                WHERE tenant_id = %s AND transfer_id = %s AND sku = %s
                """,
            )

        status = (
            "received_with_discrepancy"
            if has_discrepancy
            else "received"
        )
        rowcount = execute_write(
            db,
            """
            UPDATE inventory_transfers
            SET status = ?, receive_idempotency_key = ?,
                received_by_user_id = ?, received_by_name = ?,
                receive_note = ?, received_at = ?
            WHERE tenant_id = ? AND id = ? AND status = 'pending'
            """,
            (
                status,
                payload.idempotency_key,
                receiver["id"],
                receiver["display_name"],
                payload.note,
                timestamp,
                tenant,
                normalized_id,
            ),
            """
            UPDATE inventory_transfers
            SET status = %s, receive_idempotency_key = %s,
                received_by_user_id = %s, received_by_name = %s,
                receive_note = %s, received_at = %s
            WHERE tenant_id = %s AND id = %s AND status = 'pending'
            """,
        )
        if rowcount != 1:
            raise DomainError(
                409,
                "transfer_conflict",
                "This transfer changed while it was being confirmed. Review and retry.",
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing_by_key = _load_transfer(
            db,
            tenant,
            receive_idempotency_key=payload.idempotency_key,
        )
        if (
            existing_by_key
            and existing_by_key["id"] == normalized_id
            and receiver is not None
            and _existing_transfer_receive_matches(
                existing_by_key,
                payload,
                receiver["id"],
            )
        ):
            return _transfer_response(existing_by_key, replayed=True)
        if existing_by_key:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This confirmation key was already used for another receipt.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    transfer = _load_transfer(db, tenant, transfer_id=normalized_id)
    if transfer is None:
        raise RuntimeError("Received transfer could not be reloaded.")
    return _transfer_response(transfer, replayed=False)


def _payload_fingerprint(
    payload: Any,
    *,
    exclude: set[str] | None = None,
    confirmed_user_id: str | None = None,
    actor_user_id: str | None = None,
    action: str | None = None,
) -> str:
    content = payload.model_dump(
        mode="json",
        exclude=exclude or set(),
    )
    if confirmed_user_id is not None:
        content["confirmed_user_id"] = confirmed_user_id
    if actor_user_id is not None:
        content["actor_user_id"] = actor_user_id
    if action is not None:
        content["workflow_action"] = action
    encoded = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _order_comparison_import_fingerprint(
    payload: SellingOrderImport,
    *,
    actor_user_id: str,
    source_filename: str | None,
    source_row_numbers: list[int],
) -> str:
    content = payload.model_dump(
        mode="json",
        exclude={"idempotency_key"},
    )
    content["actor_user_id"] = actor_user_id
    content["source_filename"] = source_filename
    content["source_row_numbers"] = source_row_numbers
    content["workflow_action"] = "order-comparison:import"
    encoded = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _workflow_transaction_key(
    workflow: str,
    record_id: str,
    *parts: str,
) -> str:
    material = "\x1f".join((workflow, record_id, *parts))
    return f"{workflow}-" + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()


def _affected_inventory(
    db: Any,
    tenant: str,
    skus: set[str],
) -> list[dict[str, Any]]:
    if not skus:
        return []
    return [
        item
        for item in list_inventory(db, tenant)
        if item["sku"] in skus
    ]


def _workflow_response(
    db: Any,
    tenant: str,
    data: dict[str, Any],
    *,
    replayed: bool,
    skus: set[str],
) -> dict[str, Any]:
    return {
        "authority": "Stockpile application database",
        "data": data,
        "inventory": _affected_inventory(db, tenant, skus),
        "replayed": replayed,
    }


def _load_receipt(
    db: Any,
    tenant: str,
    *,
    receipt_id: str | None = None,
    idempotency_key: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    if (receipt_id is None) == (idempotency_key is None):
        raise ValueError("Load a receipt by exactly one identifier.")
    if receipt_id is not None:
        sqlite_where = "tenant_id = ? AND id = ?"
        postgres_where = "tenant_id = %s AND id = %s"
        params = (tenant, receipt_id)
    else:
        sqlite_where = "tenant_id = ? AND idempotency_key = ?"
        postgres_where = "tenant_id = %s AND idempotency_key = %s"
        params = (tenant, idempotency_key)
    receipt = fetchone(
        db,
        f"SELECT * FROM inventory_receipts WHERE {sqlite_where}",
        params,
        (
            f"SELECT * FROM inventory_receipts WHERE {postgres_where}"
            + (" FOR UPDATE" if for_update else "")
        ),
    )
    if receipt is None:
        return None
    receipt["lines"] = fetchall(
        db,
        """
        SELECT * FROM inventory_receipt_lines
        WHERE tenant_id = ? AND receipt_id = ?
        ORDER BY product_name, location_code
        """,
        (tenant, receipt["id"]),
        """
        SELECT * FROM inventory_receipt_lines
        WHERE tenant_id = %s AND receipt_id = %s
        ORDER BY product_name, location_code
        """,
    )
    return receipt


def receipt_public(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": receipt["id"],
        "status": receipt["status"],
        "supplier_name": receipt.get("supplier_name"),
        "reference": receipt.get("reference"),
        "source": receipt["source"],
        "receiver_user_id": receipt["receiver_user_id"],
        "receiver_name": receipt["receiver_name"],
        "created_by_user_id": receipt["created_by_user_id"],
        "created_by_name": receipt["created_by_name"],
        "note": receipt.get("note"),
        "created_at": receipt["created_at"],
        "received_at": receipt["received_at"],
        "lines": [
            {
                "sku": line["sku"],
                "barcode": line["barcode"],
                "product_name": line["product_name"],
                "location_code": line["location_code"],
                "location_name": line["location_name"],
                "location_path": line["location_path"],
                "quantity": int(line["quantity"]),
                "inventory_balance_before": int(
                    line["inventory_balance_before"]
                ),
                "inventory_balance_after": int(
                    line["inventory_balance_after"]
                ),
                "location_balance_before": int(
                    line["location_balance_before"]
                ),
                "location_balance_after": int(
                    line["location_balance_after"]
                ),
                "transaction_id": line["transaction_id"],
            }
            for line in receipt.get("lines", [])
        ],
    }


def list_receipts(
    db: Any,
    tenant: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = fetchall(
        db,
        """
        SELECT id FROM inventory_receipts
        WHERE tenant_id = ?
        ORDER BY created_at DESC LIMIT ?
        """,
        (tenant, limit),
        """
        SELECT id FROM inventory_receipts
        WHERE tenant_id = %s
        ORDER BY created_at DESC LIMIT %s
        """,
    )
    receipts: list[dict[str, Any]] = []
    for row in rows:
        receipt = _load_receipt(db, tenant, receipt_id=row["id"])
        if receipt is not None:
            receipts.append(receipt_public(receipt))
    return receipts


def create_receipt(
    db: Any,
    user: dict[str, Any],
    payload: ReceiptCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    receipt_id = str(uuid.uuid4())
    timestamp = utc_now()
    fingerprint: str | None = None
    receiver: dict[str, Any] | None = None
    try:
        begin_write(db)
        receiver = _employee_for_badge(
            db,
            tenant,
            payload.receiver_badge_code,
            payload.receiver_pin,
            for_update=True,
            badge_error_code="invalid_receiver_badge",
            pin_required_code="receiver_pin_required",
            pin_error_code="invalid_receiver_pin",
        )
        fingerprint = _payload_fingerprint(
            payload,
            exclude={
                "idempotency_key",
                "receiver_badge_code",
                "receiver_pin",
            },
            confirmed_user_id=receiver["id"],
        )
        existing = _load_receipt(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another delivery.",
                )
            db.commit()
            return _workflow_response(
                db,
                tenant,
                receipt_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )

        execute_write(
            db,
            """
            INSERT INTO inventory_receipts
                (id, tenant_id, idempotency_key, request_fingerprint,
                 supplier_name, reference, source, status,
                 receiver_user_id, receiver_name, created_by_user_id,
                 created_by_name, note, created_at, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                tenant,
                payload.idempotency_key,
                fingerprint,
                payload.supplier_name,
                payload.reference,
                payload.source,
                receiver["id"],
                receiver["display_name"],
                user["id"],
                user["display_name"],
                payload.note,
                timestamp,
                timestamp,
            ),
            """
            INSERT INTO inventory_receipts
                (id, tenant_id, idempotency_key, request_fingerprint,
                 supplier_name, reference, source, status,
                 receiver_user_id, receiver_name, created_by_user_id,
                 created_by_name, note, created_at, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'received', %s, %s, %s, %s, %s, %s, %s)
            """,
        )

        for line in sorted(
            payload.lines,
            key=lambda entry: (entry.barcode, entry.location_code),
        ):
            item = _item_for_write(db, tenant, line.barcode)
            if item is None:
                raise DomainError(
                    404,
                    "unknown_barcode",
                    f"No product matches barcode {line.barcode}.",
                )
            if not bool(item.get("active", True)):
                raise DomainError(
                    409,
                    "inactive_product",
                    f"{item['product_name']} is archived.",
                )
            location = _location_for_write(db, tenant, line.location_code)
            transaction_id = _apply_stock_delta(
                db,
                tenant=tenant,
                user=receiver,
                item=item,
                location=location,
                quantity_delta=line.quantity,
                idempotency_key=_workflow_transaction_key(
                    "delivery",
                    receipt_id,
                    item["sku"],
                    location["code"],
                ),
                reason=payload.note
                or payload.reference
                or "Delivery received",
                source="delivery",
                timestamp=timestamp,
                exchange_status="not_configured",
            )
            transaction = _load_transaction(
                db,
                tenant,
                transaction_id=transaction_id,
            )
            if transaction is None:
                raise RuntimeError("Delivery transaction could not be loaded.")
            execute_write(
                db,
                """
                INSERT INTO inventory_receipt_lines
                    (tenant_id, receipt_id, sku, barcode, product_name,
                     location_code, location_name, location_path, quantity,
                     inventory_balance_before, inventory_balance_after,
                     location_balance_before, location_balance_after,
                     transaction_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant,
                    receipt_id,
                    item["sku"],
                    item["barcode"],
                    item["product_name"],
                    location["code"],
                    location["name"],
                    location["path"],
                    line.quantity,
                    transaction["balance_before"],
                    transaction["balance_after"],
                    transaction["location_balance_before"],
                    transaction["location_balance_after"],
                    transaction_id,
                ),
                """
                INSERT INTO inventory_receipt_lines
                    (tenant_id, receipt_id, sku, barcode, product_name,
                     location_code, location_name, location_path, quantity,
                     inventory_balance_before, inventory_balance_after,
                     location_balance_before, location_balance_after,
                     transaction_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_receipt(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and fingerprint is not None
            and existing["request_fingerprint"] == fingerprint
        ):
            return _workflow_response(
                db,
                tenant,
                receipt_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another delivery.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    receipt = _load_receipt(db, tenant, receipt_id=receipt_id)
    if receipt is None:
        raise RuntimeError("Committed delivery could not be reloaded.")
    return _workflow_response(
        db,
        tenant,
        receipt_public(receipt),
        replayed=False,
        skus={line["sku"] for line in receipt["lines"]},
    )


def _load_return(
    db: Any,
    tenant: str,
    *,
    return_id: str | None = None,
    idempotency_key: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    if (return_id is None) == (idempotency_key is None):
        raise ValueError("Load a return by exactly one identifier.")
    if return_id is not None:
        sqlite_where = "tenant_id = ? AND id = ?"
        postgres_where = "tenant_id = %s AND id = %s"
        params = (tenant, return_id)
    else:
        sqlite_where = "tenant_id = ? AND idempotency_key = ?"
        postgres_where = "tenant_id = %s AND idempotency_key = %s"
        params = (tenant, idempotency_key)
    record = fetchone(
        db,
        f"SELECT * FROM inventory_returns WHERE {sqlite_where}",
        params,
        (
            f"SELECT * FROM inventory_returns WHERE {postgres_where}"
            + (" FOR UPDATE" if for_update else "")
        ),
    )
    if record is None:
        return None
    record["lines"] = fetchall(
        db,
        """
        SELECT * FROM inventory_return_lines
        WHERE tenant_id = ? AND return_id = ?
        ORDER BY product_name, location_code, condition
        """,
        (tenant, record["id"]),
        """
        SELECT * FROM inventory_return_lines
        WHERE tenant_id = %s AND return_id = %s
        ORDER BY product_name, location_code, condition
        """,
    )
    return record


def return_public(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "return_type": record["return_type"],
        "reference": record.get("reference"),
        "party_name": record.get("party_name"),
        "status": record["status"],
        "employee_user_id": record["employee_user_id"],
        "employee_name": record["employee_name"],
        "created_by_user_id": record["created_by_user_id"],
        "created_by_name": record["created_by_name"],
        "reason": record["reason"],
        "created_at": record["created_at"],
        "completed_at": record["completed_at"],
        "lines": [
            {
                "sku": line["sku"],
                "barcode": line["barcode"],
                "product_name": line["product_name"],
                "location_code": line["location_code"],
                "location_name": line["location_name"],
                "location_path": line["location_path"],
                "condition": line["condition"],
                "quantity": int(line["quantity"]),
                "inventory_balance_before": int(
                    line["inventory_balance_before"]
                ),
                "inventory_balance_after": int(
                    line["inventory_balance_after"]
                ),
                "location_balance_before": int(
                    line["location_balance_before"]
                ),
                "location_balance_after": int(
                    line["location_balance_after"]
                ),
                "transaction_id": line["transaction_id"],
                "damage_id": line.get("damage_id"),
            }
            for line in record.get("lines", [])
        ],
    }


def list_returns(
    db: Any,
    tenant: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = fetchall(
        db,
        """
        SELECT id FROM inventory_returns
        WHERE tenant_id = ?
        ORDER BY created_at DESC LIMIT ?
        """,
        (tenant, limit),
        """
        SELECT id FROM inventory_returns
        WHERE tenant_id = %s
        ORDER BY created_at DESC LIMIT %s
        """,
    )
    records: list[dict[str, Any]] = []
    for row in rows:
        record = _load_return(db, tenant, return_id=row["id"])
        if record is not None:
            records.append(return_public(record))
    return records


def _load_damage(
    db: Any,
    tenant: str,
    *,
    damage_id: str | None = None,
    create_idempotency_key: str | None = None,
    resolve_idempotency_key: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    identifiers = [
        damage_id is not None,
        create_idempotency_key is not None,
        resolve_idempotency_key is not None,
    ]
    if sum(identifiers) != 1:
        raise ValueError("Load damage by exactly one identifier.")
    if damage_id is not None:
        sqlite_where = "tenant_id = ? AND id = ?"
        postgres_where = "tenant_id = %s AND id = %s"
        value = damage_id
    elif create_idempotency_key is not None:
        sqlite_where = "tenant_id = ? AND create_idempotency_key = ?"
        postgres_where = "tenant_id = %s AND create_idempotency_key = %s"
        value = create_idempotency_key
    else:
        sqlite_where = "tenant_id = ? AND resolve_idempotency_key = ?"
        postgres_where = "tenant_id = %s AND resolve_idempotency_key = %s"
        value = resolve_idempotency_key
    return fetchone(
        db,
        f"SELECT * FROM inventory_damage WHERE {sqlite_where}",
        (tenant, value),
        (
            f"SELECT * FROM inventory_damage WHERE {postgres_where}"
            + (" FOR UPDATE" if for_update else "")
        ),
    )


def damage_public(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "sku": record["sku"],
        "barcode": record["barcode"],
        "product_name": record["product_name"],
        "location_code": record["location_code"],
        "location_name": record["location_name"],
        "location_path": record["location_path"],
        "quantity": int(record["quantity"]),
        "remaining_quantity": int(record["remaining_quantity"]),
        "category": record["category"],
        "reference": record.get("reference"),
        "reason": record["reason"],
        "status": record["status"],
        "resolution": record.get("resolution"),
        "resolution_reason": record.get("resolution_reason"),
        "source_type": record.get("source_type"),
        "source_id": record.get("source_id"),
        "reported_by_user_id": record["reported_by_user_id"],
        "reported_by_name": record["reported_by_name"],
        "created_by_user_id": record["created_by_user_id"],
        "created_by_name": record["created_by_name"],
        "resolved_by_user_id": record.get("resolved_by_user_id"),
        "resolved_by_name": record.get("resolved_by_name"),
        "resolved_actor_user_id": record.get("resolved_actor_user_id"),
        "resolved_actor_name": record.get("resolved_actor_name"),
        "inventory_balance_at_report": int(
            record["inventory_balance_at_report"]
        ),
        "location_balance_at_report": int(
            record["location_balance_at_report"]
        ),
        "inventory_balance_after_resolution": record.get(
            "inventory_balance_after_resolution"
        ),
        "location_balance_after_resolution": record.get(
            "location_balance_after_resolution"
        ),
        "resolution_transaction_id": record.get(
            "resolution_transaction_id"
        ),
        "created_at": record["created_at"],
        "resolved_at": record.get("resolved_at"),
    }


def list_damage(
    db: Any,
    tenant: str,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if status not in {None, "open", "resolved"}:
        raise DomainError(422, "invalid_damage_status", "Damage status is invalid.")
    if status is None:
        sqlite_query = (
            "SELECT * FROM inventory_damage WHERE tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        postgres_query = (
            "SELECT * FROM inventory_damage WHERE tenant_id = %s "
            "ORDER BY created_at DESC LIMIT %s"
        )
        params = (tenant, limit)
    else:
        sqlite_query = (
            "SELECT * FROM inventory_damage WHERE tenant_id = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        postgres_query = (
            "SELECT * FROM inventory_damage WHERE tenant_id = %s AND status = %s "
            "ORDER BY created_at DESC LIMIT %s"
        )
        params = (tenant, status, limit)
    return [
        damage_public(row)
        for row in fetchall(
            db,
            sqlite_query,
            params,
            postgres_query,
        )
    ]


def _insert_damage_record(
    db: Any,
    *,
    damage_id: str,
    tenant: str,
    idempotency_key: str,
    fingerprint: str,
    item: dict[str, Any],
    location: dict[str, Any],
    quantity: int,
    category: str,
    reference: str | None,
    reason: str,
    employee: dict[str, Any],
    actor: dict[str, Any],
    inventory_balance: int,
    location_balance: int,
    timestamp: str,
    source_type: str | None = None,
    source_id: str | None = None,
) -> None:
    execute_write(
        db,
        """
        INSERT INTO inventory_damage
            (id, tenant_id, create_idempotency_key, request_fingerprint,
             sku, barcode, product_name, location_code, location_name,
             location_path, quantity, remaining_quantity, category,
             reference, reason, status, source_type, source_id,
             reported_by_user_id, reported_by_name, created_by_user_id,
             created_by_name, inventory_balance_at_report,
             location_balance_at_report, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open',
                ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            damage_id,
            tenant,
            idempotency_key,
            fingerprint,
            item["sku"],
            item["barcode"],
            item["product_name"],
            location["code"],
            location["name"],
            location["path"],
            quantity,
            quantity,
            category,
            reference,
            reason,
            source_type,
            source_id,
            employee["id"],
            employee["display_name"],
            actor["id"],
            actor["display_name"],
            inventory_balance,
            location_balance,
            timestamp,
        ),
        """
        INSERT INTO inventory_damage
            (id, tenant_id, create_idempotency_key, request_fingerprint,
             sku, barcode, product_name, location_code, location_name,
             location_path, quantity, remaining_quantity, category,
             reference, reason, status, source_type, source_id,
             reported_by_user_id, reported_by_name, created_by_user_id,
             created_by_name, inventory_balance_at_report,
             location_balance_at_report, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, 'open', %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
    )


def _open_damage_rows(
    db: Any,
    tenant: str,
    sku: str,
    location_code: str,
    *,
    for_update: bool = False,
) -> list[dict[str, Any]]:
    return fetchall(
        db,
        """
        SELECT * FROM inventory_damage
        WHERE tenant_id = ? AND sku = ? AND location_code = ?
          AND status = 'open' AND remaining_quantity > 0
        ORDER BY created_at, id
        """,
        (tenant, sku, location_code),
        (
            """
            SELECT * FROM inventory_damage
            WHERE tenant_id = %s AND sku = %s AND location_code = %s
              AND status = 'open' AND remaining_quantity > 0
            ORDER BY created_at, id
            """
            + (" FOR UPDATE" if for_update else "")
        ),
    )


def _consume_damage_rows(
    db: Any,
    *,
    rows: list[dict[str, Any]],
    quantity: int,
    employee: dict[str, Any],
    actor: dict[str, Any],
    reason: str,
    inventory_balance_after: int,
    location_balance_after: int,
    timestamp: str,
) -> None:
    available = sum(int(row["remaining_quantity"]) for row in rows)
    if available < quantity:
        raise DomainError(
            409,
            "insufficient_damaged_stock",
            f"Only {available} damaged units are recorded at this location.",
        )
    remaining_to_consume = quantity
    for row in rows:
        if remaining_to_consume == 0:
            break
        current = int(row["remaining_quantity"])
        consumed = min(current, remaining_to_consume)
        remaining = current - consumed
        remaining_to_consume -= consumed
        if remaining == 0:
            rowcount = execute_write(
                db,
                """
                UPDATE inventory_damage
                SET remaining_quantity = 0, status = 'resolved',
                    resolution = 'return_to_supplier',
                    resolution_reason = ?, resolved_by_user_id = ?,
                    resolved_by_name = ?, resolved_actor_user_id = ?,
                    resolved_actor_name = ?,
                    inventory_balance_after_resolution = ?,
                    location_balance_after_resolution = ?, resolved_at = ?
                WHERE tenant_id = ? AND id = ? AND status = 'open'
                  AND remaining_quantity = ?
                """,
                (
                    reason,
                    employee["id"],
                    employee["display_name"],
                    actor["id"],
                    actor["display_name"],
                    inventory_balance_after,
                    location_balance_after,
                    timestamp,
                    row["tenant_id"],
                    row["id"],
                    current,
                ),
                """
                UPDATE inventory_damage
                SET remaining_quantity = 0, status = 'resolved',
                    resolution = 'return_to_supplier',
                    resolution_reason = %s, resolved_by_user_id = %s,
                    resolved_by_name = %s, resolved_actor_user_id = %s,
                    resolved_actor_name = %s,
                    inventory_balance_after_resolution = %s,
                    location_balance_after_resolution = %s, resolved_at = %s
                WHERE tenant_id = %s AND id = %s AND status = 'open'
                  AND remaining_quantity = %s
                """,
            )
        else:
            rowcount = execute_write(
                db,
                """
                UPDATE inventory_damage
                SET remaining_quantity = ?
                WHERE tenant_id = ? AND id = ? AND status = 'open'
                  AND remaining_quantity = ?
                """,
                (remaining, row["tenant_id"], row["id"], current),
                """
                UPDATE inventory_damage
                SET remaining_quantity = %s
                WHERE tenant_id = %s AND id = %s AND status = 'open'
                  AND remaining_quantity = %s
                """,
            )
        if rowcount != 1:
            raise DomainError(
                409,
                "damage_conflict",
                "Damaged stock changed while this return was being saved.",
            )


def create_return(
    db: Any,
    user: dict[str, Any],
    payload: ReturnCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    return_id = str(uuid.uuid4())
    timestamp = utc_now()
    fingerprint: str | None = None
    employee: dict[str, Any] | None = None
    try:
        begin_write(db)
        employee = _employee_for_badge(
            db,
            tenant,
            payload.employee_badge_code,
            payload.employee_pin,
            for_update=True,
        )
        fingerprint = _payload_fingerprint(
            payload,
            exclude={
                "idempotency_key",
                "employee_badge_code",
                "employee_pin",
            },
            confirmed_user_id=employee["id"],
        )
        existing = _load_return(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another return.",
                )
            db.commit()
            return _workflow_response(
                db,
                tenant,
                return_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )

        execute_write(
            db,
            """
            INSERT INTO inventory_returns
                (id, tenant_id, idempotency_key, request_fingerprint,
                 return_type, reference, party_name, status,
                 employee_user_id, employee_name, created_by_user_id,
                 created_by_name, reason, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                return_id,
                tenant,
                payload.idempotency_key,
                fingerprint,
                payload.return_type,
                payload.reference,
                payload.party_name,
                employee["id"],
                employee["display_name"],
                user["id"],
                user["display_name"],
                payload.reason,
                timestamp,
                timestamp,
            ),
            """
            INSERT INTO inventory_returns
                (id, tenant_id, idempotency_key, request_fingerprint,
                 return_type, reference, party_name, status,
                 employee_user_id, employee_name, created_by_user_id,
                 created_by_name, reason, created_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'completed', %s, %s, %s, %s, %s, %s, %s)
            """,
        )

        for line in sorted(
            payload.lines,
            key=lambda entry: (
                entry.barcode,
                entry.location_code,
                entry.condition,
            ),
        ):
            item = _item_for_write(db, tenant, line.barcode)
            if item is None:
                raise DomainError(
                    404,
                    "unknown_barcode",
                    f"No product matches barcode {line.barcode}.",
                )
            if not bool(item.get("active", True)):
                raise DomainError(
                    409,
                    "inactive_product",
                    f"{item['product_name']} is archived.",
                )
            location = _location_for_write(db, tenant, line.location_code)
            damage_rows: list[dict[str, Any]] = []
            if (
                payload.return_type == "supplier_return"
                and line.condition == "damaged"
            ):
                damage_rows = _open_damage_rows(
                    db,
                    tenant,
                    item["sku"],
                    location["code"],
                    for_update=True,
                )
                damaged_available = sum(
                    int(row["remaining_quantity"])
                    for row in damage_rows
                )
                if damaged_available < line.quantity:
                    raise DomainError(
                        409,
                        "insufficient_damaged_stock",
                        f"Only {damaged_available} damaged units are recorded at "
                        f"{location['name']}.",
                    )

            quantity_delta = (
                line.quantity
                if payload.return_type == "customer_return"
                else -line.quantity
            )
            transaction_id = _apply_stock_delta(
                db,
                tenant=tenant,
                user=employee,
                item=item,
                location=location,
                quantity_delta=quantity_delta,
                idempotency_key=_workflow_transaction_key(
                    "return",
                    return_id,
                    item["sku"],
                    location["code"],
                    line.condition,
                ),
                reason=payload.reason,
                source="return",
                timestamp=timestamp,
                exchange_status="not_configured",
                availability_allowance=(
                    line.quantity
                    if payload.return_type == "supplier_return"
                    and line.condition == "damaged"
                    else 0
                ),
            )
            transaction = _load_transaction(
                db,
                tenant,
                transaction_id=transaction_id,
            )
            if transaction is None:
                raise RuntimeError("Return transaction could not be loaded.")

            damage_id: str | None = None
            if (
                payload.return_type == "customer_return"
                and line.condition == "damaged"
            ):
                damage_id = str(uuid.uuid4())
                _insert_damage_record(
                    db,
                    damage_id=damage_id,
                    tenant=tenant,
                    idempotency_key=_workflow_transaction_key(
                        "return-damage",
                        return_id,
                        item["sku"],
                        location["code"],
                    ),
                    fingerprint=fingerprint,
                    item=item,
                    location=location,
                    quantity=line.quantity,
                    category="quality_issue",
                    reference=payload.reference,
                    reason=payload.reason,
                    employee=employee,
                    actor=user,
                    inventory_balance=int(transaction["balance_after"]),
                    location_balance=int(
                        transaction["location_balance_after"]
                    ),
                    timestamp=timestamp,
                    source_type="customer_return",
                    source_id=return_id,
                )
            elif damage_rows:
                _consume_damage_rows(
                    db,
                    rows=damage_rows,
                    quantity=line.quantity,
                    employee=employee,
                    actor=user,
                    reason=payload.reason,
                    inventory_balance_after=int(transaction["balance_after"]),
                    location_balance_after=int(
                        transaction["location_balance_after"]
                    ),
                    timestamp=timestamp,
                )

            execute_write(
                db,
                """
                INSERT INTO inventory_return_lines
                    (tenant_id, return_id, sku, barcode, product_name,
                     location_code, location_name, location_path, condition,
                     quantity, inventory_balance_before,
                     inventory_balance_after, location_balance_before,
                     location_balance_after, transaction_id, damage_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant,
                    return_id,
                    item["sku"],
                    item["barcode"],
                    item["product_name"],
                    location["code"],
                    location["name"],
                    location["path"],
                    line.condition,
                    line.quantity,
                    transaction["balance_before"],
                    transaction["balance_after"],
                    transaction["location_balance_before"],
                    transaction["location_balance_after"],
                    transaction_id,
                    damage_id,
                ),
                """
                INSERT INTO inventory_return_lines
                    (tenant_id, return_id, sku, barcode, product_name,
                     location_code, location_name, location_path, condition,
                     quantity, inventory_balance_before,
                     inventory_balance_after, location_balance_before,
                     location_balance_after, transaction_id, damage_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_return(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and fingerprint is not None
            and existing["request_fingerprint"] == fingerprint
        ):
            return _workflow_response(
                db,
                tenant,
                return_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another return.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    record = _load_return(db, tenant, return_id=return_id)
    if record is None:
        raise RuntimeError("Committed return could not be reloaded.")
    return _workflow_response(
        db,
        tenant,
        return_public(record),
        replayed=False,
        skus={line["sku"] for line in record["lines"]},
    )


def create_damage(
    db: Any,
    user: dict[str, Any],
    payload: DamageCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    damage_id = str(uuid.uuid4())
    timestamp = utc_now()
    fingerprint: str | None = None
    try:
        begin_write(db)
        employee = _employee_for_badge(
            db,
            tenant,
            payload.employee_badge_code,
            payload.employee_pin,
            for_update=True,
        )
        fingerprint = _payload_fingerprint(
            payload,
            exclude={
                "idempotency_key",
                "employee_badge_code",
                "employee_pin",
            },
            confirmed_user_id=employee["id"],
        )
        existing = _load_damage(
            db,
            tenant,
            create_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another damage report.",
                )
            db.commit()
            return _workflow_response(
                db,
                tenant,
                damage_public(existing),
                replayed=True,
                skus={existing["sku"]},
            )

        item = _item_for_write(db, tenant, payload.barcode)
        if item is None:
            raise DomainError(
                404,
                "unknown_barcode",
                "No product matches this barcode.",
            )
        if not bool(item.get("active", True)):
            raise DomainError(409, "inactive_product", "This product is archived.")
        location = _location_for_write(db, tenant, payload.location_code)
        position = _position_for_write(
            db,
            tenant,
            item["sku"],
            location["code"],
            timestamp,
        )
        _assert_position_total(db, tenant, item["sku"], item["current_stock"])
        _require_available_stock(
            db,
            tenant,
            item,
            position,
            payload.quantity,
        )
        _insert_damage_record(
            db,
            damage_id=damage_id,
            tenant=tenant,
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            item=item,
            location=location,
            quantity=payload.quantity,
            category=payload.category,
            reference=payload.reference,
            reason=payload.reason,
            employee=employee,
            actor=user,
            inventory_balance=int(item["current_stock"]),
            location_balance=int(position["quantity"]),
            timestamp=timestamp,
        )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_damage(
            db,
            tenant,
            create_idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and fingerprint is not None
            and existing["request_fingerprint"] == fingerprint
        ):
            return _workflow_response(
                db,
                tenant,
                damage_public(existing),
                replayed=True,
                skus={existing["sku"]},
            )
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another damage report.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    record = _load_damage(db, tenant, damage_id=damage_id)
    if record is None:
        raise RuntimeError("Committed damage report could not be reloaded.")
    return _workflow_response(
        db,
        tenant,
        damage_public(record),
        replayed=False,
        skus={record["sku"]},
    )


def resolve_damage(
    db: Any,
    user: dict[str, Any],
    damage_id: str,
    payload: DamageResolve,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_id = damage_id.strip()
    timestamp = utc_now()
    fingerprint: str | None = None
    try:
        begin_write(db)
        employee = _employee_for_badge(
            db,
            tenant,
            payload.employee_badge_code,
            payload.employee_pin,
            for_update=True,
        )
        fingerprint = _payload_fingerprint(
            payload,
            exclude={
                "idempotency_key",
                "employee_badge_code",
                "employee_pin",
            },
            confirmed_user_id=employee["id"],
        )
        existing_by_key = _load_damage(
            db,
            tenant,
            resolve_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing_by_key is not None:
            if (
                existing_by_key["id"] != normalized_id
                or existing_by_key.get("resolve_fingerprint") != fingerprint
            ):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another resolution.",
                )
            db.commit()
            return _workflow_response(
                db,
                tenant,
                damage_public(existing_by_key),
                replayed=True,
                skus={existing_by_key["sku"]},
            )

        record = _load_damage(
            db,
            tenant,
            damage_id=normalized_id,
            for_update=True,
        )
        if record is None:
            raise DomainError(404, "damage_not_found", "Damage report not found.")
        if record["status"] != "open":
            raise DomainError(
                409,
                "damage_already_resolved",
                "This damage report is already resolved.",
            )
        item = _item_by_sku_for_write(db, tenant, record["sku"])
        if item is None:
            raise DomainError(
                409,
                "missing_inventory",
                "The inventory item no longer exists.",
            )
        location = _location_for_write(
            db,
            tenant,
            record["location_code"],
            require_active_stockable=False,
        )
        quantity = int(record["remaining_quantity"])
        if quantity <= 0:
            raise DomainError(
                409,
                "damage_balance_invalid",
                "This damage report has no unresolved quantity.",
            )

        transaction_id: str | None = None
        if payload.resolution == "restock":
            position = _position_for_write(
                db,
                tenant,
                item["sku"],
                location["code"],
                timestamp,
            )
            _assert_position_total(
                db,
                tenant,
                item["sku"],
                item["current_stock"],
            )
            inventory_after = int(item["current_stock"])
            location_after = int(position["quantity"])
        else:
            transaction_id = _apply_stock_delta(
                db,
                tenant=tenant,
                user=employee,
                item=item,
                location=location,
                quantity_delta=-quantity,
                idempotency_key=_workflow_transaction_key(
                    "damage-resolution",
                    normalized_id,
                    payload.resolution,
                ),
                reason=payload.reason,
                source="damage",
                timestamp=timestamp,
                exchange_status="not_configured",
                availability_allowance=quantity,
            )
            transaction = _load_transaction(
                db,
                tenant,
                transaction_id=transaction_id,
            )
            if transaction is None:
                raise RuntimeError("Damage transaction could not be loaded.")
            inventory_after = int(transaction["balance_after"])
            location_after = int(transaction["location_balance_after"])

        rowcount = execute_write(
            db,
            """
            UPDATE inventory_damage
            SET remaining_quantity = 0, status = 'resolved',
                resolution = ?, resolution_reason = ?,
                resolve_idempotency_key = ?, resolve_fingerprint = ?,
                resolved_by_user_id = ?, resolved_by_name = ?,
                resolved_actor_user_id = ?, resolved_actor_name = ?,
                inventory_balance_after_resolution = ?,
                location_balance_after_resolution = ?,
                resolution_transaction_id = ?, resolved_at = ?
            WHERE tenant_id = ? AND id = ? AND status = 'open'
              AND remaining_quantity = ?
            """,
            (
                payload.resolution,
                payload.reason,
                payload.idempotency_key,
                fingerprint,
                employee["id"],
                employee["display_name"],
                user["id"],
                user["display_name"],
                inventory_after,
                location_after,
                transaction_id,
                timestamp,
                tenant,
                normalized_id,
                quantity,
            ),
            """
            UPDATE inventory_damage
            SET remaining_quantity = 0, status = 'resolved',
                resolution = %s, resolution_reason = %s,
                resolve_idempotency_key = %s, resolve_fingerprint = %s,
                resolved_by_user_id = %s, resolved_by_name = %s,
                resolved_actor_user_id = %s, resolved_actor_name = %s,
                inventory_balance_after_resolution = %s,
                location_balance_after_resolution = %s,
                resolution_transaction_id = %s, resolved_at = %s
            WHERE tenant_id = %s AND id = %s AND status = 'open'
              AND remaining_quantity = %s
            """,
        )
        if rowcount != 1:
            raise DomainError(
                409,
                "damage_conflict",
                "This damage report changed. Review and retry.",
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_damage(
            db,
            tenant,
            resolve_idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and existing["id"] == normalized_id
            and fingerprint is not None
            and existing.get("resolve_fingerprint") == fingerprint
        ):
            return _workflow_response(
                db,
                tenant,
                damage_public(existing),
                replayed=True,
                skus={existing["sku"]},
            )
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another resolution.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    resolved = _load_damage(db, tenant, damage_id=normalized_id)
    if resolved is None:
        raise RuntimeError("Resolved damage report could not be reloaded.")
    return _workflow_response(
        db,
        tenant,
        damage_public(resolved),
        replayed=False,
        skus={resolved["sku"]},
    )


def _investigation_location_snapshot(
    db: Any,
    tenant: str,
    code: str,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    normalized_code = code.strip().upper()
    if not normalized_code or normalized_code == "UNASSIGNED":
        raise DomainError(
            422,
            "invalid_investigation_location",
            "Choose a recorded stock location.",
        )

    rows = fetchall(
        db,
        """
        SELECT code, name, kind, parent_code, active, stockable
        FROM locations
        WHERE tenant_id = ?
        ORDER BY code
        """,
        (tenant,),
        (
            """
            SELECT code, name, kind, parent_code, active, stockable
            FROM locations
            WHERE tenant_id = %s
            ORDER BY code
            """
            + (" FOR SHARE" if for_update else "")
        ),
    )
    by_code = {row["code"]: row for row in rows}
    location = by_code.get(normalized_code)
    if location is None:
        raise DomainError(
            404,
            "location_not_found",
            "Location not found.",
        )
    location["path"] = _location_paths(rows)[normalized_code]
    return location


def _investigation_position_snapshot(
    db: Any,
    tenant: str,
    sku: str,
    location_code: str,
    *,
    for_update: bool = False,
) -> dict[str, int]:
    position = fetchone(
        db,
        """
        SELECT quantity, version
        FROM inventory_positions
        WHERE tenant_id = ? AND sku = ? AND location_code = ?
        """,
        (tenant, sku, location_code),
        (
            """
            SELECT quantity, version
            FROM inventory_positions
            WHERE tenant_id = %s AND sku = %s AND location_code = %s
            """
            + (" FOR SHARE" if for_update else "")
        ),
    )
    if position is None:
        return {
            "quantity": 0,
            "version": 0,
        }
    return {
        "quantity": int(position["quantity"]),
        "version": int(position.get("version", 0)),
    }


def _load_investigation_event_by_key(
    db: Any,
    tenant: str,
    idempotency_key: str,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    return fetchone(
        db,
        """
        SELECT *
        FROM inventory_investigation_events
        WHERE tenant_id = ? AND idempotency_key = ?
        """,
        (tenant, idempotency_key),
        (
            """
            SELECT *
            FROM inventory_investigation_events
            WHERE tenant_id = %s AND idempotency_key = %s
            """
            + (" FOR UPDATE" if for_update else "")
        ),
    )


def _load_investigation(
    db: Any,
    tenant: str,
    investigation_id: str,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    record = fetchone(
        db,
        """
        SELECT *
        FROM inventory_investigations
        WHERE tenant_id = ? AND id = ?
        """,
        (tenant, investigation_id),
        (
            """
            SELECT *
            FROM inventory_investigations
            WHERE tenant_id = %s AND id = %s
            """
            + (" FOR UPDATE" if for_update else "")
        ),
    )
    if record is None:
        return None
    record["events"] = fetchall(
        db,
        """
        SELECT *
        FROM inventory_investigation_events
        WHERE tenant_id = ? AND investigation_id = ?
        ORDER BY sequence, created_at, id
        """,
        (tenant, investigation_id),
        """
        SELECT *
        FROM inventory_investigation_events
        WHERE tenant_id = %s AND investigation_id = %s
        ORDER BY sequence, created_at, id
        """,
    )
    return record


def _investigation_event_public(
    event: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": event["id"],
        "sequence": int(event["sequence"]),
        "event_type": event["event_type"],
        "location_code": event.get("location_code"),
        "location_name": event.get("location_name"),
        "location_path": event.get("location_path"),
        "reference": event.get("reference"),
        "note": event.get("note"),
        "reason": event.get("reason"),
        "resolution": event.get("resolution"),
        "employee_user_id": event["employee_user_id"],
        "employee_name": event["employee_name"],
        "actor_user_id": event["actor_user_id"],
        "actor_name": event["actor_name"],
        "created_at": event["created_at"],
    }


def investigation_public(
    record: dict[str, Any],
) -> dict[str, Any]:
    expected_balance = record.get(
        "expected_location_balance_at_open"
    )
    expected_version = record.get(
        "expected_location_version_at_open"
    )
    observed_balance = record.get(
        "observed_location_balance_at_open"
    )
    observed_version = record.get(
        "observed_location_version_at_open"
    )
    return {
        "id": record["id"],
        "kind": record["kind"],
        "status": record["status"],
        "sku": record["sku"],
        "barcode": record["barcode"],
        "product_name": record["product_name"],
        "quantity": int(record["quantity"]),
        "expected_location_code": record.get(
            "expected_location_code"
        ),
        "expected_location_name": record.get(
            "expected_location_name"
        ),
        "expected_location_path": record.get(
            "expected_location_path"
        ),
        "observed_location_code": record.get(
            "observed_location_code"
        ),
        "observed_location_name": record.get(
            "observed_location_name"
        ),
        "observed_location_path": record.get(
            "observed_location_path"
        ),
        "reference": record.get("reference"),
        "note": record.get("note"),
        "inventory_balance_at_open": int(
            record["inventory_balance_at_open"]
        ),
        "inventory_version_at_open": int(
            record["inventory_version_at_open"]
        ),
        "expected_location_balance_at_open": (
            int(expected_balance)
            if expected_balance is not None
            else None
        ),
        "expected_location_version_at_open": (
            int(expected_version)
            if expected_version is not None
            else None
        ),
        "observed_location_balance_at_open": (
            int(observed_balance)
            if observed_balance is not None
            else None
        ),
        "observed_location_version_at_open": (
            int(observed_version)
            if observed_version is not None
            else None
        ),
        "opened_employee_user_id": record[
            "opened_employee_user_id"
        ],
        "opened_employee_name": record[
            "opened_employee_name"
        ],
        "opened_actor_user_id": record[
            "opened_actor_user_id"
        ],
        "opened_actor_name": record[
            "opened_actor_name"
        ],
        "resolution": record.get("resolution"),
        "resolution_reference": record.get(
            "resolution_reference"
        ),
        "resolution_reason": record.get(
            "resolution_reason"
        ),
        "resolved_location_code": record.get(
            "resolved_location_code"
        ),
        "resolved_location_name": record.get(
            "resolved_location_name"
        ),
        "resolved_location_path": record.get(
            "resolved_location_path"
        ),
        "resolved_employee_user_id": record.get(
            "resolved_employee_user_id"
        ),
        "resolved_employee_name": record.get(
            "resolved_employee_name"
        ),
        "resolved_actor_user_id": record.get(
            "resolved_actor_user_id"
        ),
        "resolved_actor_name": record.get(
            "resolved_actor_name"
        ),
        "created_at": record["created_at"],
        "resolved_at": record.get("resolved_at"),
        "events": [
            _investigation_event_public(event)
            for event in record.get("events", [])
        ],
    }


def _investigation_response(
    record: dict[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "data": investigation_public(record),
        "replayed": replayed,
    }


def _investigation_replay(
    db: Any,
    tenant: str,
    *,
    idempotency_key: str,
    fingerprint: str,
    event_type: str,
    investigation_id: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    event = _load_investigation_event_by_key(
        db,
        tenant,
        idempotency_key,
        for_update=for_update,
    )
    if event is None:
        return None
    if (
        event["request_fingerprint"] != fingerprint
        or event["event_type"] != event_type
        or (
            investigation_id is not None
            and event["investigation_id"] != investigation_id
        )
    ):
        raise DomainError(
            409,
            "idempotency_conflict",
            "This submission key was already used for another investigation action.",
        )
    record = _load_investigation(
        db,
        tenant,
        event["investigation_id"],
        for_update=for_update,
    )
    if record is None:
        raise RuntimeError(
            "Investigation event references a missing investigation."
        )
    return _investigation_response(
        record,
        replayed=True,
    )


def _next_investigation_event_sequence(
    db: Any,
    tenant: str,
    investigation_id: str,
) -> int:
    row = fetchone(
        db,
        """
        SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
        FROM inventory_investigation_events
        WHERE tenant_id = ? AND investigation_id = ?
        """,
        (tenant, investigation_id),
        """
        SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
        FROM inventory_investigation_events
        WHERE tenant_id = %s AND investigation_id = %s
        """,
    )
    if row is None:
        raise RuntimeError(
            "Investigation event sequence could not be allocated."
        )
    return int(row["next_sequence"])


def _insert_investigation_event(
    db: Any,
    *,
    tenant: str,
    investigation_id: str,
    sequence: int,
    event_type: str,
    idempotency_key: str,
    fingerprint: str,
    location: dict[str, Any] | None,
    reference: str | None,
    note: str | None,
    reason: str | None,
    resolution: str | None,
    employee: dict[str, Any],
    actor: dict[str, Any],
    timestamp: str,
) -> None:
    execute_write(
        db,
        """
        INSERT INTO inventory_investigation_events
            (id, tenant_id, investigation_id, sequence, event_type,
             idempotency_key, request_fingerprint, location_code,
             location_name, location_path, reference, note, reason,
             resolution, employee_user_id, employee_name,
             actor_user_id, actor_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            tenant,
            investigation_id,
            sequence,
            event_type,
            idempotency_key,
            fingerprint,
            location["code"] if location is not None else None,
            location["name"] if location is not None else None,
            location["path"] if location is not None else None,
            reference,
            note,
            reason,
            resolution,
            employee["id"],
            employee["display_name"],
            actor["id"],
            actor["display_name"],
            timestamp,
        ),
        """
        INSERT INTO inventory_investigation_events
            (id, tenant_id, investigation_id, sequence, event_type,
             idempotency_key, request_fingerprint, location_code,
             location_name, location_path, reference, note, reason,
             resolution, employee_user_id, employee_name,
             actor_user_id, actor_name, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
    )


def list_investigations(
    db: Any,
    tenant: str,
    status: str | None = None,
    kind: str | None = None,
    location_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if status not in {None, "open", "resolved"}:
        raise DomainError(
            422,
            "invalid_investigation_status",
            "Investigation status is invalid.",
        )
    if kind not in {
        None,
        "missing",
        "misplaced",
        "unexpected",
    }:
        raise DomainError(
            422,
            "invalid_investigation_kind",
            "Investigation kind is invalid.",
        )
    if limit < 1 or limit > 500:
        raise DomainError(
            422,
            "invalid_investigation_limit",
            "Investigation limit must be between 1 and 500.",
        )

    normalized_location: str | None = None
    if location_code is not None:
        normalized_location = location_code.strip().upper()
        if (
            not normalized_location
            or normalized_location == "UNASSIGNED"
        ):
            raise DomainError(
                422,
                "invalid_investigation_location",
                "Choose a recorded stock location.",
            )

    sqlite_conditions = ["tenant_id = ?"]
    postgres_conditions = ["tenant_id = %s"]
    params: list[Any] = [tenant]
    if status is not None:
        sqlite_conditions.append("status = ?")
        postgres_conditions.append("status = %s")
        params.append(status)
    if kind is not None:
        sqlite_conditions.append("kind = ?")
        postgres_conditions.append("kind = %s")
        params.append(kind)
    if normalized_location is not None:
        sqlite_conditions.append(
            "(expected_location_code = ? "
            "OR observed_location_code = ? "
            "OR resolved_location_code = ?)"
        )
        postgres_conditions.append(
            "(expected_location_code = %s "
            "OR observed_location_code = %s "
            "OR resolved_location_code = %s)"
        )
        params.extend(
            [
                normalized_location,
                normalized_location,
                normalized_location,
            ]
        )
    params.append(limit)

    rows = fetchall(
        db,
        "SELECT id FROM inventory_investigations WHERE "
        + " AND ".join(sqlite_conditions)
        + " ORDER BY created_at DESC, id DESC LIMIT ?",
        tuple(params),
        "SELECT id FROM inventory_investigations WHERE "
        + " AND ".join(postgres_conditions)
        + " ORDER BY created_at DESC, id DESC LIMIT %s",
    )
    records: list[dict[str, Any]] = []
    for row in rows:
        record = _load_investigation(
            db,
            tenant,
            row["id"],
        )
        if record is not None:
            records.append(investigation_public(record))
    return records


def create_investigation(
    db: Any,
    user: dict[str, Any],
    payload: InvestigationCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    investigation_id = str(uuid.uuid4())
    timestamp = utc_now()
    fingerprint: str | None = None
    try:
        begin_write(db)
        employee = _employee_for_badge(
            db,
            tenant,
            payload.employee_badge_code,
            payload.employee_pin,
            for_update=True,
        )
        fingerprint = _payload_fingerprint(
            payload,
            exclude={
                "idempotency_key",
                "employee_badge_code",
                "employee_pin",
            },
            confirmed_user_id=employee["id"],
            actor_user_id=user["id"],
            action="investigation:open",
        )
        replay = _investigation_replay(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            event_type="opened",
            for_update=True,
        )
        if replay is not None:
            db.commit()
            return replay

        item = _item_for_write(
            db,
            tenant,
            payload.barcode,
        )
        if item is None:
            raise DomainError(
                404,
                "unknown_barcode",
                "No product matches this barcode.",
            )

        replay = _investigation_replay(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            event_type="opened",
            for_update=True,
        )
        if replay is not None:
            db.commit()
            return replay

        expected_location = (
            _investigation_location_snapshot(
                db,
                tenant,
                payload.expected_location_code,
                for_update=True,
            )
            if payload.expected_location_code is not None
            else None
        )
        observed_location = (
            _investigation_location_snapshot(
                db,
                tenant,
                payload.observed_location_code,
                for_update=True,
            )
            if payload.observed_location_code is not None
            else None
        )
        expected_position = (
            _investigation_position_snapshot(
                db,
                tenant,
                item["sku"],
                expected_location["code"],
                for_update=True,
            )
            if expected_location is not None
            else None
        )
        observed_position = (
            _investigation_position_snapshot(
                db,
                tenant,
                item["sku"],
                observed_location["code"],
                for_update=True,
            )
            if observed_location is not None
            else None
        )

        execute_write(
            db,
            """
            INSERT INTO inventory_investigations
                (id, tenant_id, create_idempotency_key,
                 request_fingerprint, kind, status, sku, barcode,
                 product_name, quantity, expected_location_code,
                 expected_location_name, expected_location_path,
                 observed_location_code, observed_location_name,
                 observed_location_path, reference, note,
                 inventory_balance_at_open, inventory_version_at_open,
                 expected_location_balance_at_open,
                 expected_location_version_at_open,
                 observed_location_balance_at_open,
                 observed_location_version_at_open,
                 opened_employee_user_id, opened_employee_name,
                 opened_actor_user_id, opened_actor_name, created_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                investigation_id,
                tenant,
                payload.idempotency_key,
                fingerprint,
                payload.kind,
                item["sku"],
                item["barcode"],
                item["product_name"],
                payload.quantity,
                (
                    expected_location["code"]
                    if expected_location is not None
                    else None
                ),
                (
                    expected_location["name"]
                    if expected_location is not None
                    else None
                ),
                (
                    expected_location["path"]
                    if expected_location is not None
                    else None
                ),
                (
                    observed_location["code"]
                    if observed_location is not None
                    else None
                ),
                (
                    observed_location["name"]
                    if observed_location is not None
                    else None
                ),
                (
                    observed_location["path"]
                    if observed_location is not None
                    else None
                ),
                payload.reference,
                payload.note,
                int(item["current_stock"]),
                int(item.get("version", 0)),
                (
                    expected_position["quantity"]
                    if expected_position is not None
                    else None
                ),
                (
                    expected_position["version"]
                    if expected_position is not None
                    else None
                ),
                (
                    observed_position["quantity"]
                    if observed_position is not None
                    else None
                ),
                (
                    observed_position["version"]
                    if observed_position is not None
                    else None
                ),
                employee["id"],
                employee["display_name"],
                user["id"],
                user["display_name"],
                timestamp,
            ),
            """
            INSERT INTO inventory_investigations
                (id, tenant_id, create_idempotency_key,
                 request_fingerprint, kind, status, sku, barcode,
                 product_name, quantity, expected_location_code,
                 expected_location_name, expected_location_path,
                 observed_location_code, observed_location_name,
                 observed_location_path, reference, note,
                 inventory_balance_at_open, inventory_version_at_open,
                 expected_location_balance_at_open,
                 expected_location_version_at_open,
                 observed_location_balance_at_open,
                 observed_location_version_at_open,
                 opened_employee_user_id, opened_employee_name,
                 opened_actor_user_id, opened_actor_name, created_at)
            VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
        )
        primary_location = observed_location or expected_location
        _insert_investigation_event(
            db,
            tenant=tenant,
            investigation_id=investigation_id,
            sequence=1,
            event_type="opened",
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            location=primary_location,
            reference=payload.reference,
            note=payload.note,
            reason=None,
            resolution=None,
            employee=employee,
            actor=user,
            timestamp=timestamp,
        )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        if fingerprint is not None:
            replay = _investigation_replay(
                db,
                tenant,
                idempotency_key=payload.idempotency_key,
                fingerprint=fingerprint,
                event_type="opened",
            )
            if replay is not None:
                return replay
        raise
    except Exception:
        db.rollback()
        raise

    record = _load_investigation(
        db,
        tenant,
        investigation_id,
    )
    if record is None:
        raise RuntimeError(
            "Committed investigation could not be reloaded."
        )
    return _investigation_response(
        record,
        replayed=False,
    )


def add_investigation_evidence(
    db: Any,
    user: dict[str, Any],
    investigation_id: str,
    payload: InvestigationEvidenceCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_id = investigation_id.strip()
    timestamp = utc_now()
    fingerprint: str | None = None
    try:
        begin_write(db)
        employee = _employee_for_badge(
            db,
            tenant,
            payload.employee_badge_code,
            payload.employee_pin,
            for_update=True,
        )
        fingerprint = _payload_fingerprint(
            payload,
            exclude={
                "idempotency_key",
                "employee_badge_code",
                "employee_pin",
            },
            confirmed_user_id=employee["id"],
            actor_user_id=user["id"],
            action=f"investigation:evidence:{normalized_id}",
        )
        replay = _investigation_replay(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            event_type="evidence_added",
            investigation_id=normalized_id,
            for_update=True,
        )
        if replay is not None:
            db.commit()
            return replay

        record = _load_investigation(
            db,
            tenant,
            normalized_id,
            for_update=True,
        )
        if record is None:
            raise DomainError(
                404,
                "investigation_not_found",
                "Investigation not found.",
            )

        replay = _investigation_replay(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            event_type="evidence_added",
            investigation_id=normalized_id,
            for_update=True,
        )
        if replay is not None:
            db.commit()
            return replay
        if record["status"] != "open":
            raise DomainError(
                409,
                "investigation_already_resolved",
                "This investigation is already resolved.",
            )

        location = (
            _investigation_location_snapshot(
                db,
                tenant,
                payload.location_code,
                for_update=True,
            )
            if payload.location_code is not None
            else None
        )
        sequence = _next_investigation_event_sequence(
            db,
            tenant,
            normalized_id,
        )
        _insert_investigation_event(
            db,
            tenant=tenant,
            investigation_id=normalized_id,
            sequence=sequence,
            event_type="evidence_added",
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            location=location,
            reference=None,
            note=payload.note,
            reason=None,
            resolution=None,
            employee=employee,
            actor=user,
            timestamp=timestamp,
        )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        if fingerprint is not None:
            replay = _investigation_replay(
                db,
                tenant,
                idempotency_key=payload.idempotency_key,
                fingerprint=fingerprint,
                event_type="evidence_added",
                investigation_id=normalized_id,
            )
            if replay is not None:
                return replay
        raise
    except Exception:
        db.rollback()
        raise

    record = _load_investigation(
        db,
        tenant,
        normalized_id,
    )
    if record is None:
        raise RuntimeError(
            "Updated investigation could not be reloaded."
        )
    return _investigation_response(
        record,
        replayed=False,
    )


def resolve_investigation(
    db: Any,
    user: dict[str, Any],
    investigation_id: str,
    payload: InvestigationResolve,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_id = investigation_id.strip()
    timestamp = utc_now()
    fingerprint: str | None = None
    try:
        begin_write(db)
        employee = _employee_for_badge(
            db,
            tenant,
            payload.employee_badge_code,
            payload.employee_pin,
            for_update=True,
        )
        fingerprint = _payload_fingerprint(
            payload,
            exclude={
                "idempotency_key",
                "employee_badge_code",
                "employee_pin",
            },
            confirmed_user_id=employee["id"],
            actor_user_id=user["id"],
            action=f"investigation:resolve:{normalized_id}",
        )
        replay = _investigation_replay(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            event_type="resolved",
            investigation_id=normalized_id,
            for_update=True,
        )
        if replay is not None:
            db.commit()
            return replay

        record = _load_investigation(
            db,
            tenant,
            normalized_id,
            for_update=True,
        )
        if record is None:
            raise DomainError(
                404,
                "investigation_not_found",
                "Investigation not found.",
            )

        replay = _investigation_replay(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            event_type="resolved",
            investigation_id=normalized_id,
            for_update=True,
        )
        if replay is not None:
            db.commit()
            return replay
        if record["status"] != "open":
            raise DomainError(
                409,
                "investigation_already_resolved",
                "This investigation is already resolved.",
            )

        resolved_location = (
            _investigation_location_snapshot(
                db,
                tenant,
                payload.resolved_location_code,
                for_update=True,
            )
            if payload.resolved_location_code is not None
            else None
        )
        sequence = _next_investigation_event_sequence(
            db,
            tenant,
            normalized_id,
        )
        rowcount = execute_write(
            db,
            """
            UPDATE inventory_investigations
            SET status = 'resolved', resolve_idempotency_key = ?,
                resolve_fingerprint = ?, resolution = ?,
                resolution_reference = ?, resolution_reason = ?,
                resolved_location_code = ?, resolved_location_name = ?,
                resolved_location_path = ?,
                resolved_employee_user_id = ?,
                resolved_employee_name = ?, resolved_actor_user_id = ?,
                resolved_actor_name = ?, resolved_at = ?
            WHERE tenant_id = ? AND id = ? AND status = 'open'
            """,
            (
                payload.idempotency_key,
                fingerprint,
                payload.resolution,
                payload.reference,
                payload.reason,
                (
                    resolved_location["code"]
                    if resolved_location is not None
                    else None
                ),
                (
                    resolved_location["name"]
                    if resolved_location is not None
                    else None
                ),
                (
                    resolved_location["path"]
                    if resolved_location is not None
                    else None
                ),
                employee["id"],
                employee["display_name"],
                user["id"],
                user["display_name"],
                timestamp,
                tenant,
                normalized_id,
            ),
            """
            UPDATE inventory_investigations
            SET status = 'resolved', resolve_idempotency_key = %s,
                resolve_fingerprint = %s, resolution = %s,
                resolution_reference = %s, resolution_reason = %s,
                resolved_location_code = %s, resolved_location_name = %s,
                resolved_location_path = %s,
                resolved_employee_user_id = %s,
                resolved_employee_name = %s, resolved_actor_user_id = %s,
                resolved_actor_name = %s, resolved_at = %s
            WHERE tenant_id = %s AND id = %s AND status = 'open'
            """,
        )
        if rowcount != 1:
            raise DomainError(
                409,
                "investigation_conflict",
                "This investigation changed. Review and retry.",
            )
        _insert_investigation_event(
            db,
            tenant=tenant,
            investigation_id=normalized_id,
            sequence=sequence,
            event_type="resolved",
            idempotency_key=payload.idempotency_key,
            fingerprint=fingerprint,
            location=resolved_location,
            reference=payload.reference,
            note=None,
            reason=payload.reason,
            resolution=payload.resolution,
            employee=employee,
            actor=user,
            timestamp=timestamp,
        )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        if fingerprint is not None:
            replay = _investigation_replay(
                db,
                tenant,
                idempotency_key=payload.idempotency_key,
                fingerprint=fingerprint,
                event_type="resolved",
                investigation_id=normalized_id,
            )
            if replay is not None:
                return replay
        raise
    except Exception:
        db.rollback()
        raise

    record = _load_investigation(
        db,
        tenant,
        normalized_id,
    )
    if record is None:
        raise RuntimeError(
            "Resolved investigation could not be reloaded."
        )
    return _investigation_response(
        record,
        replayed=False,
    )


def _load_reservation(
    db: Any,
    tenant: str,
    *,
    reservation_id: str | None = None,
    create_idempotency_key: str | None = None,
    close_idempotency_key: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    identifiers = [
        reservation_id is not None,
        create_idempotency_key is not None,
        close_idempotency_key is not None,
    ]
    if sum(identifiers) != 1:
        raise ValueError("Load a reservation by exactly one identifier.")
    if reservation_id is not None:
        sqlite_where = "tenant_id = ? AND id = ?"
        postgres_where = "tenant_id = %s AND id = %s"
        value = reservation_id
    elif create_idempotency_key is not None:
        sqlite_where = "tenant_id = ? AND create_idempotency_key = ?"
        postgres_where = "tenant_id = %s AND create_idempotency_key = %s"
        value = create_idempotency_key
    else:
        sqlite_where = "tenant_id = ? AND close_idempotency_key = ?"
        postgres_where = "tenant_id = %s AND close_idempotency_key = %s"
        value = close_idempotency_key
    reservation = fetchone(
        db,
        f"SELECT * FROM inventory_reservations WHERE {sqlite_where}",
        (tenant, value),
        (
            f"SELECT * FROM inventory_reservations WHERE {postgres_where}"
            + (" FOR UPDATE" if for_update else "")
        ),
    )
    if reservation is None:
        return None
    reservation["lines"] = fetchall(
        db,
        """
        SELECT * FROM inventory_reservation_lines
        WHERE tenant_id = ? AND reservation_id = ?
        ORDER BY product_name, location_code
        """,
        (tenant, reservation["id"]),
        """
        SELECT * FROM inventory_reservation_lines
        WHERE tenant_id = %s AND reservation_id = %s
        ORDER BY product_name, location_code
        """,
    )
    return reservation


def reservation_public(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "reference": record["reference"],
        "customer_name": record.get("customer_name"),
        "sales_channel": record.get("sales_channel"),
        "status": record["status"],
        "note": record.get("note"),
        "close_action": record.get("close_action"),
        "close_reason": record.get("close_reason"),
        "created_by_user_id": record["created_by_user_id"],
        "created_by_name": record["created_by_name"],
        "closed_by_user_id": record.get("closed_by_user_id"),
        "closed_by_name": record.get("closed_by_name"),
        "closed_actor_user_id": record.get("closed_actor_user_id"),
        "closed_actor_name": record.get("closed_actor_name"),
        "created_at": record["created_at"],
        "closed_at": record.get("closed_at"),
        "total_quantity": sum(
            int(line["quantity"])
            for line in record.get("lines", [])
        ),
        "lines": [
            {
                "sku": line["sku"],
                "barcode": line["barcode"],
                "product_name": line["product_name"],
                "location_code": line["location_code"],
                "location_name": line["location_name"],
                "location_path": line["location_path"],
                "quantity": int(line["quantity"]),
                "on_hand_at_create": int(line["on_hand_at_create"]),
                "reserved_before": int(line["reserved_before"]),
                "reserved_after": int(line["reserved_after"]),
                "available_before": int(line["available_before"]),
                "available_after": int(line["available_after"]),
                "inventory_balance_after_fulfillment": line.get(
                    "inventory_balance_after_fulfillment"
                ),
                "location_balance_after_fulfillment": line.get(
                    "location_balance_after_fulfillment"
                ),
                "fulfillment_transaction_id": line.get(
                    "fulfillment_transaction_id"
                ),
            }
            for line in record.get("lines", [])
        ],
    }


def list_reservations(
    db: Any,
    tenant: str,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if status not in {None, "active", "released", "fulfilled"}:
        raise DomainError(
            422,
            "invalid_reservation_status",
            "Reservation status is invalid.",
        )
    if status is None:
        sqlite_query = (
            "SELECT id FROM inventory_reservations WHERE tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        postgres_query = (
            "SELECT id FROM inventory_reservations WHERE tenant_id = %s "
            "ORDER BY created_at DESC LIMIT %s"
        )
        params = (tenant, limit)
    else:
        sqlite_query = (
            "SELECT id FROM inventory_reservations "
            "WHERE tenant_id = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        postgres_query = (
            "SELECT id FROM inventory_reservations "
            "WHERE tenant_id = %s AND status = %s "
            "ORDER BY created_at DESC LIMIT %s"
        )
        params = (tenant, status, limit)
    records: list[dict[str, Any]] = []
    for row in fetchall(
        db,
        sqlite_query,
        params,
        postgres_query,
    ):
        record = _load_reservation(
            db,
            tenant,
            reservation_id=row["id"],
        )
        if record is not None:
            records.append(reservation_public(record))
    return records


def create_reservation(
    db: Any,
    user: dict[str, Any],
    payload: ReservationCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    reservation_id = str(uuid.uuid4())
    timestamp = utc_now()
    fingerprint = _payload_fingerprint(
        payload,
        exclude={"idempotency_key"},
    )
    try:
        begin_write(db)
        existing = _load_reservation(
            db,
            tenant,
            create_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another reservation.",
                )
            db.commit()
            return _workflow_response(
                db,
                tenant,
                reservation_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )

        execute_write(
            db,
            """
            INSERT INTO inventory_reservations
                (id, tenant_id, create_idempotency_key,
                 request_fingerprint, reference, customer_name,
                 sales_channel, status, note, created_by_user_id,
                 created_by_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                reservation_id,
                tenant,
                payload.idempotency_key,
                fingerprint,
                payload.reference,
                payload.customer_name,
                payload.sales_channel,
                payload.note,
                user["id"],
                user["display_name"],
                timestamp,
            ),
            """
            INSERT INTO inventory_reservations
                (id, tenant_id, create_idempotency_key,
                 request_fingerprint, reference, customer_name,
                 sales_channel, status, note, created_by_user_id,
                 created_by_name, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s)
            """,
        )

        for line in sorted(
            payload.lines,
            key=lambda entry: (entry.barcode, entry.location_code),
        ):
            item = _item_for_write(db, tenant, line.barcode)
            if item is None:
                raise DomainError(
                    404,
                    "unknown_barcode",
                    f"No product matches barcode {line.barcode}.",
                )
            if not bool(item.get("active", True)):
                raise DomainError(
                    409,
                    "inactive_product",
                    f"{item['product_name']} is archived.",
                )
            location = _location_for_write(db, tenant, line.location_code)
            position = _position_for_write(
                db,
                tenant,
                item["sku"],
                location["code"],
                timestamp,
            )
            _assert_position_total(
                db,
                tenant,
                item["sku"],
                item["current_stock"],
            )
            commitments = _require_available_stock(
                db,
                tenant,
                item,
                position,
                line.quantity,
            )
            execute_write(
                db,
                """
                INSERT INTO inventory_reservation_lines
                    (tenant_id, reservation_id, sku, barcode, product_name,
                     location_code, location_name, location_path, quantity,
                     on_hand_at_create, reserved_before, reserved_after,
                     available_before, available_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant,
                    reservation_id,
                    item["sku"],
                    item["barcode"],
                    item["product_name"],
                    location["code"],
                    location["name"],
                    location["path"],
                    line.quantity,
                    position["quantity"],
                    commitments["reserved"],
                    commitments["reserved"] + line.quantity,
                    commitments["available"],
                    commitments["available"] - line.quantity,
                ),
                """
                INSERT INTO inventory_reservation_lines
                    (tenant_id, reservation_id, sku, barcode, product_name,
                     location_code, location_name, location_path, quantity,
                     on_hand_at_create, reserved_before, reserved_after,
                     available_before, available_after)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_reservation(
            db,
            tenant,
            create_idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and existing["request_fingerprint"] == fingerprint
        ):
            return _workflow_response(
                db,
                tenant,
                reservation_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another reservation.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    reservation = _load_reservation(
        db,
        tenant,
        reservation_id=reservation_id,
    )
    if reservation is None:
        raise RuntimeError("Committed reservation could not be reloaded.")
    return _workflow_response(
        db,
        tenant,
        reservation_public(reservation),
        replayed=False,
        skus={line["sku"] for line in reservation["lines"]},
    )


def close_reservation(
    db: Any,
    user: dict[str, Any],
    reservation_id: str,
    payload: ReservationClose,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_id = reservation_id.strip()
    timestamp = utc_now()
    fingerprint: str | None = None
    try:
        begin_write(db)
        if payload.action == "fulfill":
            employee = _employee_for_badge(
                db,
                tenant,
                payload.employee_badge_code or "",
                payload.employee_pin,
                for_update=True,
            )
        else:
            employee = user
        fingerprint = _payload_fingerprint(
            payload,
            exclude={
                "idempotency_key",
                "employee_badge_code",
                "employee_pin",
            },
            confirmed_user_id=employee["id"],
        )
        existing_by_key = _load_reservation(
            db,
            tenant,
            close_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing_by_key is not None:
            if (
                existing_by_key["id"] != normalized_id
                or existing_by_key.get("close_fingerprint") != fingerprint
            ):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another reservation action.",
                )
            db.commit()
            return _workflow_response(
                db,
                tenant,
                reservation_public(existing_by_key),
                replayed=True,
                skus={line["sku"] for line in existing_by_key["lines"]},
            )

        reservation = _load_reservation(
            db,
            tenant,
            reservation_id=normalized_id,
            for_update=True,
        )
        if reservation is None:
            raise DomainError(
                404,
                "reservation_not_found",
                "Reservation not found.",
            )
        if reservation["status"] != "active":
            raise DomainError(
                409,
                "reservation_already_closed",
                "This reservation is already closed.",
            )

        if payload.action == "fulfill":
            for line in sorted(
                reservation["lines"],
                key=lambda entry: (entry["sku"], entry["location_code"]),
            ):
                item = _item_by_sku_for_write(db, tenant, line["sku"])
                if item is None:
                    raise DomainError(
                        409,
                        "missing_inventory",
                        f"{line['product_name']} no longer exists.",
                    )
                location = _location_for_write(
                    db,
                    tenant,
                    line["location_code"],
                    require_active_stockable=False,
                )
                transaction_id = _apply_stock_delta(
                    db,
                    tenant=tenant,
                    user=employee,
                    item=item,
                    location=location,
                    quantity_delta=-int(line["quantity"]),
                    idempotency_key=_workflow_transaction_key(
                        "reservation",
                        normalized_id,
                        line["sku"],
                        line["location_code"],
                    ),
                    reason=payload.reason
                    or f"Fulfilled reservation {reservation['reference']}",
                    source="reservation",
                    timestamp=timestamp,
                    exchange_status="not_configured",
                    availability_allowance=int(line["quantity"]),
                )
                transaction = _load_transaction(
                    db,
                    tenant,
                    transaction_id=transaction_id,
                )
                if transaction is None:
                    raise RuntimeError(
                        "Reservation transaction could not be loaded."
                    )
                execute_write(
                    db,
                    """
                    UPDATE inventory_reservation_lines
                    SET inventory_balance_after_fulfillment = ?,
                        location_balance_after_fulfillment = ?,
                        fulfillment_transaction_id = ?
                    WHERE tenant_id = ? AND reservation_id = ?
                      AND sku = ? AND location_code = ?
                    """,
                    (
                        transaction["balance_after"],
                        transaction["location_balance_after"],
                        transaction_id,
                        tenant,
                        normalized_id,
                        line["sku"],
                        line["location_code"],
                    ),
                    """
                    UPDATE inventory_reservation_lines
                    SET inventory_balance_after_fulfillment = %s,
                        location_balance_after_fulfillment = %s,
                        fulfillment_transaction_id = %s
                    WHERE tenant_id = %s AND reservation_id = %s
                      AND sku = %s AND location_code = %s
                    """,
                )

        status = "fulfilled" if payload.action == "fulfill" else "released"
        rowcount = execute_write(
            db,
            """
            UPDATE inventory_reservations
            SET status = ?, close_action = ?, close_reason = ?,
                close_idempotency_key = ?, close_fingerprint = ?,
                closed_by_user_id = ?, closed_by_name = ?,
                closed_actor_user_id = ?, closed_actor_name = ?,
                closed_at = ?
            WHERE tenant_id = ? AND id = ? AND status = 'active'
            """,
            (
                status,
                payload.action,
                payload.reason,
                payload.idempotency_key,
                fingerprint,
                employee["id"],
                employee["display_name"],
                user["id"],
                user["display_name"],
                timestamp,
                tenant,
                normalized_id,
            ),
            """
            UPDATE inventory_reservations
            SET status = %s, close_action = %s, close_reason = %s,
                close_idempotency_key = %s, close_fingerprint = %s,
                closed_by_user_id = %s, closed_by_name = %s,
                closed_actor_user_id = %s, closed_actor_name = %s,
                closed_at = %s
            WHERE tenant_id = %s AND id = %s AND status = 'active'
            """,
        )
        if rowcount != 1:
            raise DomainError(
                409,
                "reservation_conflict",
                "This reservation changed. Review and retry.",
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_reservation(
            db,
            tenant,
            close_idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and existing["id"] == normalized_id
            and fingerprint is not None
            and existing.get("close_fingerprint") == fingerprint
        ):
            return _workflow_response(
                db,
                tenant,
                reservation_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another reservation action.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    closed = _load_reservation(
        db,
        tenant,
        reservation_id=normalized_id,
    )
    if closed is None:
        raise RuntimeError("Closed reservation could not be reloaded.")
    return _workflow_response(
        db,
        tenant,
        reservation_public(closed),
        replayed=False,
        skus={line["sku"] for line in closed["lines"]},
    )


SELLING_ORDER_CSV_ALIASES = {
    "order_id": "order_reference",
    "order_no": "order_reference",
    "order_number": "order_reference",
    "seller_sku": "sku",
    "merchant_sku": "sku",
    "product_sku": "sku",
    "variation_sku": "sku",
    "product_barcode": "barcode",
    "qty": "quantity",
}


def _comparison_text_key(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


def _begin_order_comparison_import(db: Any) -> None:
    begin_write(db)


def _begin_order_comparison_resolution(db: Any) -> None:
    begin_write(db)
    execute_write(
        db,
        "SELECT 1",
        (),
        """
        LOCK TABLE inventory,
                   inventory_reservations,
                   inventory_reservation_lines,
                   inventory_transactions
        IN SHARE MODE
        """,
    )


def _parse_selling_order_csv(
    csv_text: str,
    *,
    sales_channel: str,
    idempotency_key: str,
) -> tuple[SellingOrderImport, list[SellingOrderCsvRow]]:
    reader = csv.DictReader(io.StringIO(csv_text, newline=""))
    if reader.fieldnames is None:
        raise DomainError(422, "invalid_csv", "The CSV header row is missing.")

    headers = [
        SELLING_ORDER_CSV_ALIASES.get(
            _normalize_csv_header(header or ""),
            _normalize_csv_header(header or ""),
        )
        for header in reader.fieldnames
    ]
    if any(not header for header in headers) or len(headers) != len(set(headers)):
        raise DomainError(
            422,
            "invalid_csv_headers",
            "CSV column names must be present and unique.",
        )
    reader.fieldnames = headers

    required = {"order_reference", "quantity"}
    missing = sorted(required.difference(headers))
    if missing:
        raise DomainError(
            422,
            "missing_csv_columns",
            f"Missing CSV columns: {', '.join(missing)}.",
        )
    if "sku" not in headers and "barcode" not in headers:
        raise DomainError(
            422,
            "missing_csv_columns",
            "The CSV requires a SKU or barcode column.",
        )

    parsed: list[SellingOrderCsvRow] = []
    errors: list[str] = []
    nonempty_rows = 0

    try:
        for line_number, raw_row in enumerate(reader, start=2):
            if None in raw_row:
                errors.append(f"Row {line_number}: too many columns.")
                continue
            values = {
                key: (value.strip() if isinstance(value, str) else value)
                for key, value in raw_row.items()
                if key is not None
            }
            if not any(value for value in values.values()):
                continue

            nonempty_rows += 1
            if nonempty_rows > 5_000:
                raise DomainError(
                    422,
                    "too_many_csv_rows",
                    "Import at most 5,000 order lines at a time.",
                )

            try:
                raw_quantity = str(values.get("quantity") or "").replace(
                    ",", ""
                )
                if not raw_quantity or any(
                    character not in "0123456789"
                    for character in raw_quantity
                ):
                    raise ValueError("quantity must be a positive whole number")
                quantity = int(raw_quantity)
                if quantity <= 0:
                    raise ValueError("quantity must be greater than zero")
                if quantity > 1_000_000:
                    raise ValueError("quantity cannot exceed 1,000,000")

                order = SellingOrderLine(
                    order_reference=str(values.get("order_reference") or ""),
                    sku=(str(values.get("sku") or "").strip() or None),
                    barcode=(
                        str(values.get("barcode") or "").strip() or None
                    ),
                    quantity=quantity,
                )
            except ValidationError as error:
                errors.append(
                    f"Row {line_number}: {_validation_message(error)}."
                )
                continue
            except ValueError as error:
                errors.append(f"Row {line_number}: {error}.")
                continue

            parsed.append(
                SellingOrderCsvRow(
                    line_number=line_number,
                    order=order,
                )
            )
    except csv.Error as error:
        raise DomainError(
            422,
            "invalid_csv",
            "The order CSV could not be read.",
        ) from error

    if nonempty_rows == 0:
        raise DomainError(422, "empty_csv", "The CSV contains no order lines.")
    if errors:
        visible = errors[:8]
        if len(errors) > len(visible):
            visible.append(f"{len(errors) - len(visible)} more row errors.")
        raise DomainError(
            422,
            "invalid_order_rows",
            " ".join(visible),
        )

    try:
        payload = SellingOrderImport(
            sales_channel=sales_channel,
            lines=[row.order for row in parsed],
            idempotency_key=idempotency_key,
        )
    except ValidationError as error:
        raise DomainError(
            422,
            "invalid_order_import",
            _validation_message(error),
        ) from error

    return payload, parsed


def _current_order_evidence(
    db: Any,
    tenant: str,
    *,
    batch_id: str,
    sales_channel: str,
) -> dict[str, dict[str, Any]]:
    rows = fetchall(
        db,
        """
        SELECT compared.id AS comparison_line_id,
               reserved.sku AS compared_sku,
               reservation.id AS candidate_reservation_id,
               reservation.status AS candidate_reservation_status,
               reservation.sales_channel AS candidate_sales_channel,
               reservation.created_at AS candidate_reservation_created_at,
               reserved.quantity AS reserved_quantity,
               reserved.fulfillment_transaction_id,
               movement.movement_type AS fulfillment_movement_type,
               movement.source AS fulfillment_source,
               movement.sku AS fulfillment_sku,
               movement.quantity AS fulfillment_quantity,
               movement.reversed_by_transaction_id AS fulfillment_reversed_by
        FROM order_comparison_lines compared
        LEFT JOIN inventory submitted_sku_product
          ON submitted_sku_product.tenant_id = compared.tenant_id
         AND submitted_sku_product.sku = compared.submitted_sku
        LEFT JOIN inventory submitted_barcode_product
          ON submitted_barcode_product.tenant_id = compared.tenant_id
         AND submitted_barcode_product.barcode = compared.submitted_barcode
        JOIN inventory_reservations reservation
          ON reservation.tenant_id = compared.tenant_id
         AND LOWER(TRIM(reservation.reference)) =
             LOWER(TRIM(compared.order_reference))
        JOIN inventory_reservation_lines reserved
          ON reserved.tenant_id = reservation.tenant_id
         AND reserved.reservation_id = reservation.id
         AND reserved.sku = CASE
             WHEN compared.submitted_sku IS NOT NULL
              AND compared.submitted_barcode IS NOT NULL
              AND submitted_sku_product.sku = submitted_barcode_product.sku
             THEN submitted_sku_product.sku
             WHEN compared.submitted_sku IS NOT NULL
              AND compared.submitted_barcode IS NULL
             THEN submitted_sku_product.sku
             WHEN compared.submitted_sku IS NULL
              AND compared.submitted_barcode IS NOT NULL
             THEN submitted_barcode_product.sku
             ELSE NULL
         END
        LEFT JOIN inventory_transactions movement
          ON movement.tenant_id = reserved.tenant_id
         AND movement.id = reserved.fulfillment_transaction_id
        WHERE compared.tenant_id = ? AND compared.batch_id = ?
        ORDER BY compared.source_row_number,
                 reservation.created_at DESC,
                 reservation.id,
                 reserved.location_code
        """,
        (tenant, batch_id),
        """
        SELECT compared.id AS comparison_line_id,
               reserved.sku AS compared_sku,
               reservation.id AS candidate_reservation_id,
               reservation.status AS candidate_reservation_status,
               reservation.sales_channel AS candidate_sales_channel,
               reservation.created_at AS candidate_reservation_created_at,
               reserved.quantity AS reserved_quantity,
               reserved.fulfillment_transaction_id,
               movement.movement_type AS fulfillment_movement_type,
               movement.source AS fulfillment_source,
               movement.sku AS fulfillment_sku,
               movement.quantity AS fulfillment_quantity,
               movement.reversed_by_transaction_id AS fulfillment_reversed_by
        FROM order_comparison_lines compared
        LEFT JOIN inventory submitted_sku_product
          ON submitted_sku_product.tenant_id = compared.tenant_id
         AND submitted_sku_product.sku = compared.submitted_sku
        LEFT JOIN inventory submitted_barcode_product
          ON submitted_barcode_product.tenant_id = compared.tenant_id
         AND submitted_barcode_product.barcode = compared.submitted_barcode
        JOIN inventory_reservations reservation
          ON reservation.tenant_id = compared.tenant_id
         AND LOWER(TRIM(reservation.reference)) =
             LOWER(TRIM(compared.order_reference))
        JOIN inventory_reservation_lines reserved
          ON reserved.tenant_id = reservation.tenant_id
         AND reserved.reservation_id = reservation.id
         AND reserved.sku = CASE
             WHEN compared.submitted_sku IS NOT NULL
              AND compared.submitted_barcode IS NOT NULL
              AND submitted_sku_product.sku = submitted_barcode_product.sku
             THEN submitted_sku_product.sku
             WHEN compared.submitted_sku IS NOT NULL
              AND compared.submitted_barcode IS NULL
             THEN submitted_sku_product.sku
             WHEN compared.submitted_sku IS NULL
              AND compared.submitted_barcode IS NOT NULL
             THEN submitted_barcode_product.sku
             ELSE NULL
         END
        LEFT JOIN inventory_transactions movement
          ON movement.tenant_id = reserved.tenant_id
         AND movement.id = reserved.fulfillment_transaction_id
        WHERE compared.tenant_id = %s AND compared.batch_id = %s
        ORDER BY compared.source_row_number,
                 reservation.created_at DESC,
                 reservation.id,
                 reserved.location_code
        """,
    )

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        comparison_id = row["comparison_line_id"]
        reservation_id = row["candidate_reservation_id"]
        reservation = grouped.setdefault(comparison_id, {}).setdefault(
            reservation_id,
            {
                "id": reservation_id,
                "status": row["candidate_reservation_status"],
                "sales_channel": row.get("candidate_sales_channel"),
                "created_at": row["candidate_reservation_created_at"],
                "lines": [],
            },
        )
        reservation["lines"].append(row)

    channel_key = _comparison_text_key(sales_channel)
    evidence: dict[str, dict[str, Any]] = {}
    for comparison_id, candidate_map in grouped.items():
        candidates = list(candidate_map.values())
        exact = [
            candidate
            for candidate in candidates
            if _comparison_text_key(candidate.get("sales_channel"))
            == channel_key
        ]
        if exact:
            qualified = exact
        elif (
            len(candidates) == 1
            and not _comparison_text_key(candidates[0].get("sales_channel"))
        ):
            qualified = candidates
        else:
            qualified = []

        if len(qualified) > 1:
            evidence[comparison_id] = {
                "current_reservation_id": None,
                "current_reservation_status": None,
                "current_reservation_quantity": 0,
                "current_evidence_issue": "ambiguous_order_reference",
            }
            continue
        if not qualified:
            continue

        reservation = qualified[0]
        quantity = 0
        issue: str | None = None
        if reservation["status"] == "fulfilled":
            for reserved in reservation["lines"]:
                if not reserved.get("fulfillment_transaction_id"):
                    issue = issue or "incomplete_fulfillment"
                    continue
                if reserved.get("fulfillment_reversed_by"):
                    issue = issue or "reversed_removal"
                    continue
                if (
                    reserved.get("fulfillment_movement_type") != "stock_out"
                    or reserved.get("fulfillment_source") != "reservation"
                    or reserved.get("fulfillment_sku")
                    != reserved.get("compared_sku")
                    or int(reserved.get("fulfillment_quantity") or 0)
                    != int(reserved["reserved_quantity"])
                ):
                    issue = issue or "inconsistent_fulfillment"
                    continue
                quantity += int(reserved["fulfillment_quantity"])

        if quantity > 1_000_000:
            issue = issue or "quantity_limit_exceeded"
        evidence[comparison_id] = {
            "current_reservation_id": reservation["id"],
            "current_reservation_status": reservation["status"],
            "current_reservation_quantity": quantity,
            "current_evidence_issue": issue,
        }
    return evidence


def _attach_current_product_mapping(line: dict[str, Any]) -> None:
    submitted_sku = line.get("submitted_sku")
    submitted_barcode = line.get("submitted_barcode")
    sku_match = line.get("submitted_sku_match")
    barcode_match = line.get("submitted_barcode_match")
    issue: str | None = None

    if submitted_sku is not None and submitted_barcode is not None:
        if sku_match is not None and sku_match == barcode_match:
            current_sku = sku_match
        elif sku_match is None and barcode_match is None:
            current_sku = None
        else:
            current_sku = None
            issue = "product_identifier_conflict"
    elif submitted_sku is not None:
        current_sku = sku_match
    else:
        current_sku = barcode_match

    line["current_product_sku"] = current_sku
    line["current_product_mapping_issue"] = issue


def _load_order_comparison(
    db: Any,
    tenant: str,
    *,
    batch_id: str | None = None,
    idempotency_key: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    if (batch_id is None) == (idempotency_key is None):
        raise ValueError("Load an order comparison by exactly one identifier.")
    if batch_id is not None:
        sqlite_where = "tenant_id = ? AND id = ?"
        postgres_where = "tenant_id = %s AND id = %s"
        value = batch_id
    else:
        sqlite_where = "tenant_id = ? AND idempotency_key = ?"
        postgres_where = "tenant_id = %s AND idempotency_key = %s"
        value = idempotency_key

    batch = fetchone(
        db,
        f"SELECT * FROM order_comparison_batches WHERE {sqlite_where}",
        (tenant, value),
        (
            f"SELECT * FROM order_comparison_batches WHERE {postgres_where}"
            + (" FOR UPDATE" if for_update else "")
        ),
    )
    if batch is None:
        return None

    lines = fetchall(
        db,
        """
        SELECT line.*,
               submitted_sku_product.sku AS submitted_sku_match,
               submitted_barcode_product.sku AS submitted_barcode_match,
               linked.id AS linked_transaction_record_id,
               linked.quantity AS linked_quantity,
               linked.movement_type AS linked_movement_type,
               linked.source AS linked_source,
               linked.sku AS linked_sku,
               linked.reason AS linked_transaction_reason,
               linked.actor_name AS linked_transaction_actor,
               linked.created_at AS linked_transaction_created_at,
               linked.reversed_by_transaction_id AS linked_transaction_reversed_by
        FROM order_comparison_lines line
        LEFT JOIN inventory submitted_sku_product
          ON submitted_sku_product.tenant_id = line.tenant_id
         AND submitted_sku_product.sku = line.submitted_sku
        LEFT JOIN inventory submitted_barcode_product
          ON submitted_barcode_product.tenant_id = line.tenant_id
         AND submitted_barcode_product.barcode = line.submitted_barcode
        LEFT JOIN inventory_transactions linked
          ON linked.tenant_id = line.tenant_id
         AND linked.id = line.inventory_transaction_id
        WHERE line.tenant_id = ? AND line.batch_id = ?
        ORDER BY line.source_row_number
        """,
        (tenant, batch["id"]),
        """
        SELECT line.*,
               submitted_sku_product.sku AS submitted_sku_match,
               submitted_barcode_product.sku AS submitted_barcode_match,
               linked.id AS linked_transaction_record_id,
               linked.quantity AS linked_quantity,
               linked.movement_type AS linked_movement_type,
               linked.source AS linked_source,
               linked.sku AS linked_sku,
               linked.reason AS linked_transaction_reason,
               linked.actor_name AS linked_transaction_actor,
               linked.created_at AS linked_transaction_created_at,
               linked.reversed_by_transaction_id AS linked_transaction_reversed_by
        FROM order_comparison_lines line
        LEFT JOIN inventory submitted_sku_product
          ON submitted_sku_product.tenant_id = line.tenant_id
         AND submitted_sku_product.sku = line.submitted_sku
        LEFT JOIN inventory submitted_barcode_product
          ON submitted_barcode_product.tenant_id = line.tenant_id
         AND submitted_barcode_product.barcode = line.submitted_barcode
        LEFT JOIN inventory_transactions linked
          ON linked.tenant_id = line.tenant_id
         AND linked.id = line.inventory_transaction_id
        WHERE line.tenant_id = %s AND line.batch_id = %s
        ORDER BY line.source_row_number
        """,
    )
    current = _current_order_evidence(
        db,
        tenant,
        batch_id=batch["id"],
        sales_channel=batch["sales_channel"],
    )
    for line in lines:
        _attach_current_product_mapping(line)
        line.update(
            current.get(
                line["id"],
                {
                    "current_reservation_id": None,
                    "current_reservation_status": None,
                    "current_reservation_quantity": 0,
                    "current_evidence_issue": None,
                },
            )
        )
    batch["lines"] = lines
    return batch


def _load_order_comparison_line(
    db: Any,
    tenant: str,
    *,
    line_id: str | None = None,
    resolve_idempotency_key: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    if (line_id is None) == (resolve_idempotency_key is None):
        raise ValueError("Load a comparison line by exactly one identifier.")
    if line_id is not None:
        sqlite_where = "tenant_id = ? AND id = ?"
        postgres_where = "tenant_id = %s AND id = %s"
        value = line_id
    else:
        sqlite_where = "tenant_id = ? AND resolve_idempotency_key = ?"
        postgres_where = "tenant_id = %s AND resolve_idempotency_key = %s"
        value = resolve_idempotency_key

    return fetchone(
        db,
        f"SELECT * FROM order_comparison_lines WHERE {sqlite_where}",
        (tenant, value),
        (
            f"SELECT * FROM order_comparison_lines WHERE {postgres_where}"
            + (" FOR UPDATE" if for_update else "")
        ),
    )


def order_comparison_line_public(line: dict[str, Any]) -> dict[str, Any]:
    snapshot_quantity = int(line.get("stockpile_quantity") or 0)
    current_reservation_quantity = int(
        line.get("current_reservation_quantity") or 0
    )
    linked_quantity = int(line.get("linked_quantity") or 0)
    resolution = line.get("resolution")
    linked_transaction_valid = bool(
        resolution == "link_removal"
        and line.get("linked_transaction_record_id")
        and not line.get("linked_transaction_reversed_by")
        and line.get("linked_movement_type") == "stock_out"
        and line.get("linked_source") in {"scanner", "manual"}
        and line.get("current_product_sku") == line.get("sku")
        and line.get("linked_sku") == line.get("current_product_sku")
    )
    current_quantity = current_reservation_quantity + (
        linked_quantity if linked_transaction_valid else 0
    )
    if resolution == "link_removal":
        match_source = "manual_link"
    elif line.get("current_reservation_id"):
        match_source = "reservation"
    else:
        match_source = "none"

    if (
        line.get("current_product_sku") is None
        or line.get("current_product_mapping_issue")
    ):
        current_status = "unknown_product"
    elif current_quantity == int(line["ordered_quantity"]):
        current_status = "matched"
    elif current_quantity == 0:
        current_status = "missing_removal"
    else:
        current_status = "quantity_mismatch"

    evidence_changed = bool(
        current_reservation_quantity != snapshot_quantity
        or line.get("current_reservation_id") != line.get("reservation_id")
        or line.get("current_product_sku") != line.get("sku")
        or line.get("current_product_mapping_issue")
    )
    linked_record_changed = bool(
        resolution == "link_removal" and not linked_transaction_valid
    )
    stale = evidence_changed or linked_record_changed

    linked_transaction = None
    if line.get("inventory_transaction_id"):
        linked_transaction = {
            "id": line["inventory_transaction_id"],
            "quantity": linked_quantity,
            "reason": line.get("linked_transaction_reason"),
            "actor_name": line.get("linked_transaction_actor"),
            "created_at": line.get("linked_transaction_created_at"),
            "reversed": bool(line.get("linked_transaction_reversed_by")),
            "valid": linked_transaction_valid,
        }

    return {
        "id": line["id"],
        "source_row_number": int(line["source_row_number"]),
        "order_reference": line["order_reference"],
        "submitted_sku": line.get("submitted_sku"),
        "submitted_barcode": line.get("submitted_barcode"),
        "sku": line.get("sku"),
        "barcode": line.get("barcode"),
        "product_name": line.get("product_name"),
        "current_product_sku": line.get("current_product_sku"),
        "product_mapping_issue": line.get("current_product_mapping_issue"),
        "ordered_quantity": int(line["ordered_quantity"]),
        "stockpile_quantity": snapshot_quantity,
        "effective_stockpile_quantity": current_quantity,
        "comparison_status": line["comparison_status"],
        "current_comparison_status": current_status,
        "match_source": match_source,
        "reservation_id": line.get("reservation_id"),
        "current_reservation_id": line.get("current_reservation_id"),
        "reservation_status": line.get("current_reservation_status"),
        "current_reservation_quantity": current_reservation_quantity,
        "evidence_issue": line.get("current_evidence_issue"),
        "resolution": resolution,
        "resolution_reason": line.get("resolution_reason"),
        "resolved_by_user_id": line.get("resolved_by_user_id"),
        "resolved_by_name": line.get("resolved_by_name"),
        "resolved_at": line.get("resolved_at"),
        "linked_transaction": linked_transaction,
        "resolved": resolution is not None,
        "stale": stale,
        "needs_attention": stale
        or (
            resolution is None
            and (
                line["comparison_status"] != "matched"
                or line.get("current_evidence_issue") is not None
                or line.get("current_product_mapping_issue") is not None
            )
        ),
        "created_at": line["created_at"],
    }


def order_comparison_public(batch: dict[str, Any]) -> dict[str, Any]:
    lines = [
        order_comparison_line_public(line)
        for line in batch.get("lines", [])
    ]
    matched = sum(
        1 for line in lines if line["comparison_status"] == "matched"
    )
    issues = len(lines) - matched
    open_issues = sum(1 for line in lines if line["needs_attention"])
    stale = sum(1 for line in lines if line["stale"])
    current_matched = sum(
        1
        for line in lines
        if line["current_comparison_status"] == "matched"
        and not line["stale"]
    )
    resolved_issues = sum(
        1
        for line in lines
        if line["comparison_status"] != "matched"
        and line["resolved"]
        and not line["stale"]
    )
    return {
        "id": batch["id"],
        "sales_channel": batch["sales_channel"],
        "source_filename": batch.get("source_filename"),
        "imported_by_user_id": batch["imported_by_user_id"],
        "imported_by_name": batch["imported_by_name"],
        "created_at": batch["created_at"],
        "checked_at": batch["created_at"],
        "summary": {
            "lines": len(lines),
            "matched": matched,
            "current_matched": current_matched,
            "issues": issues,
            "open_issues": open_issues,
            "resolved_issues": resolved_issues,
            "stale": stale,
        },
        "lines": lines,
    }


def _order_comparison_response(
    batch: dict[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "authority": "Stockpile application database",
        "data": order_comparison_public(batch),
        "replayed": replayed,
    }


def list_order_comparisons(
    db: Any,
    tenant: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 100:
        raise DomainError(
            422,
            "invalid_comparison_limit",
            "Comparison limit must be between 1 and 100.",
        )
    batches: list[dict[str, Any]] = []
    for row in fetchall(
        db,
        """
        SELECT id FROM order_comparison_batches
        WHERE tenant_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (tenant, limit),
        """
        SELECT id FROM order_comparison_batches
        WHERE tenant_id = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
    ):
        batch = _load_order_comparison(
            db,
            tenant,
            batch_id=row["id"],
        )
        if batch is not None:
            batches.append(order_comparison_public(batch))
    return batches


def _selling_order_product(
    db: Any,
    tenant: str,
    line: SellingOrderLine,
) -> dict[str, Any] | None:
    by_sku = (
        _product_by_sku(db, tenant, line.sku)
        if line.sku is not None
        else None
    )
    by_barcode = (
        _product_by_barcode(db, tenant, line.barcode)
        if line.barcode is not None
        else None
    )

    if line.sku is not None and line.barcode is not None:
        if by_sku is None and by_barcode is None:
            return None
        if (
            by_sku is None
            or by_barcode is None
            or by_sku["sku"] != by_barcode["sku"]
        ):
            raise DomainError(
                422,
                "product_identifier_conflict",
                f"Order {line.order_reference} has a SKU and barcode that "
                "do not identify the same product.",
            )
        return by_sku

    return by_sku or by_barcode


def _order_removal(
    db: Any,
    tenant: str,
    transaction_id: str,
) -> dict[str, Any] | None:
    return fetchone(
        db,
        """
        SELECT * FROM inventory_transactions
        WHERE tenant_id = ? AND id = ?
        """,
        (tenant, transaction_id),
        """
        SELECT * FROM inventory_transactions
        WHERE tenant_id = %s AND id = %s
        """,
    )


def _fulfilled_order_quantity(
    db: Any,
    tenant: str,
    *,
    sales_channel: str,
    order_reference: str,
    sku: str,
) -> tuple[int, str | None]:
    reservations = fetchall(
        db,
        """
        SELECT reservation.id, reservation.status,
               reservation.sales_channel, reservation.created_at
        FROM inventory_reservations reservation
        WHERE reservation.tenant_id = ?
          AND LOWER(TRIM(reservation.reference)) = LOWER(TRIM(?))
          AND EXISTS (
              SELECT 1
              FROM inventory_reservation_lines line
              WHERE line.tenant_id = reservation.tenant_id
                AND line.reservation_id = reservation.id
                AND line.sku = ?
          )
        ORDER BY reservation.created_at DESC
        """,
        (tenant, order_reference, sku),
        """
        SELECT reservation.id, reservation.status,
               reservation.sales_channel, reservation.created_at
        FROM inventory_reservations reservation
        WHERE reservation.tenant_id = %s
          AND LOWER(TRIM(reservation.reference)) = LOWER(TRIM(%s))
          AND EXISTS (
              SELECT 1
              FROM inventory_reservation_lines line
              WHERE line.tenant_id = reservation.tenant_id
                AND line.reservation_id = reservation.id
                AND line.sku = %s
          )
        ORDER BY reservation.created_at DESC
        """,
    )
    if not reservations:
        return 0, None

    channel_key = _comparison_text_key(sales_channel)
    exact_channel = [
        reservation
        for reservation in reservations
        if _comparison_text_key(reservation.get("sales_channel")) == channel_key
    ]
    if exact_channel:
        candidates = exact_channel
    elif (
        len(reservations) == 1
        and not _comparison_text_key(reservations[0].get("sales_channel"))
    ):
        candidates = reservations
    else:
        return 0, None

    if len(candidates) > 1:
        raise DomainError(
            409,
            "ambiguous_order_reference",
            f"More than one Stockpile order matches {order_reference}. "
            "Correct the duplicate order records first.",
        )

    reservation = candidates[0]
    if reservation["status"] != "fulfilled":
        return 0, reservation["id"]

    quantity = 0
    lines = fetchall(
        db,
        """
        SELECT quantity, fulfillment_transaction_id
        FROM inventory_reservation_lines
        WHERE tenant_id = ? AND reservation_id = ? AND sku = ?
        ORDER BY location_code
        """,
        (tenant, reservation["id"], sku),
        """
        SELECT quantity, fulfillment_transaction_id
        FROM inventory_reservation_lines
        WHERE tenant_id = %s AND reservation_id = %s AND sku = %s
        ORDER BY location_code
        """,
    )
    for line in lines:
        transaction_id = line.get("fulfillment_transaction_id")
        if not transaction_id:
            continue
        transaction = _order_removal(
            db,
            tenant,
            transaction_id,
        )
        if transaction is None or transaction.get("reversed_by_transaction_id"):
            continue
        if (
            transaction["movement_type"] != "stock_out"
            or transaction["source"] != "reservation"
            or transaction["sku"] != sku
            or int(transaction["quantity"]) != int(line["quantity"])
        ):
            raise DomainError(
                409,
                "invalid_order_removal",
                f"The recorded removal for {order_reference} is inconsistent.",
            )
        quantity += int(transaction["quantity"])

    if quantity > 1_000_000:
        raise DomainError(
            409,
            "comparison_quantity_too_large",
            f"Recorded removals for {order_reference} exceed the supported limit.",
        )
    return quantity, reservation["id"]


def create_order_comparison(
    db: Any,
    user: dict[str, Any],
    payload: SellingOrderImport,
    *,
    source_filename: str | None = None,
    source_row_numbers: list[int] | None = None,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_filename = (source_filename or "").strip() or None
    if normalized_filename is not None and len(normalized_filename) > 255:
        raise DomainError(
            422,
            "invalid_source_filename",
            "The source filename is too long.",
        )
    row_numbers = source_row_numbers or list(
        range(2, len(payload.lines) + 2)
    )
    if (
        len(row_numbers) != len(payload.lines)
        or len(row_numbers) != len(set(row_numbers))
        or any(number < 2 for number in row_numbers)
    ):
        raise ValueError("Order comparison source rows are invalid.")

    fingerprint = _order_comparison_import_fingerprint(
        payload,
        actor_user_id=user["id"],
        source_filename=normalized_filename,
        source_row_numbers=row_numbers,
    )
    batch_id = str(uuid.uuid4())
    timestamp = utc_now()

    try:
        _begin_order_comparison_import(db)
        existing = _load_order_comparison(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another order check.",
                )
            db.commit()
            return _order_comparison_response(existing, replayed=True)

        execute_write(
            db,
            """
            INSERT INTO order_comparison_batches
                (id, tenant_id, idempotency_key, request_fingerprint,
                 sales_channel, source_filename, imported_by_user_id,
                 imported_by_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                tenant,
                payload.idempotency_key,
                fingerprint,
                payload.sales_channel,
                normalized_filename,
                user["id"],
                user["display_name"],
                timestamp,
            ),
            """
            INSERT INTO order_comparison_batches
                (id, tenant_id, idempotency_key, request_fingerprint,
                 sales_channel, source_filename, imported_by_user_id,
                 imported_by_name, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
        )

        seen_products: set[tuple[str, str]] = set()
        for line, source_row_number in zip(
            payload.lines,
            row_numbers,
            strict=True,
        ):
            item = _selling_order_product(db, tenant, line)
            if item is None:
                sku = None
                barcode = None
                product_name = None
                stockpile_quantity = 0
                reservation_id = None
                comparison_status = "unknown_product"
            else:
                canonical_key = (
                    _comparison_text_key(line.order_reference),
                    item["sku"],
                )
                if canonical_key in seen_products:
                    raise DomainError(
                        422,
                        "duplicate_order_line",
                        f"Combine duplicate lines for order {line.order_reference} "
                        f"and SKU {item['sku']}.",
                    )
                seen_products.add(canonical_key)
                sku = item["sku"]
                barcode = item["barcode"]
                product_name = item["product_name"]
                stockpile_quantity, reservation_id = _fulfilled_order_quantity(
                    db,
                    tenant,
                    sales_channel=payload.sales_channel,
                    order_reference=line.order_reference,
                    sku=sku,
                )
                if stockpile_quantity == line.quantity:
                    comparison_status = "matched"
                elif stockpile_quantity == 0:
                    comparison_status = "missing_removal"
                else:
                    comparison_status = "quantity_mismatch"

            execute_write(
                db,
                """
                INSERT INTO order_comparison_lines
                    (id, tenant_id, batch_id, source_row_number,
                     order_reference, submitted_sku, submitted_barcode,
                     sku, barcode, product_name, ordered_quantity,
                     stockpile_quantity, comparison_status, reservation_id,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    tenant,
                    batch_id,
                    source_row_number,
                    line.order_reference,
                    line.sku,
                    line.barcode,
                    sku,
                    barcode,
                    product_name,
                    line.quantity,
                    stockpile_quantity,
                    comparison_status,
                    reservation_id,
                    timestamp,
                ),
                """
                INSERT INTO order_comparison_lines
                    (id, tenant_id, batch_id, source_row_number,
                     order_reference, submitted_sku, submitted_barcode,
                     sku, barcode, product_name, ordered_quantity,
                     stockpile_quantity, comparison_status, reservation_id,
                     created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_order_comparison(
            db,
            tenant,
            idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and existing["request_fingerprint"] == fingerprint
        ):
            return _order_comparison_response(existing, replayed=True)
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another order check.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    batch = _load_order_comparison(
        db,
        tenant,
        batch_id=batch_id,
    )
    if batch is None:
        raise RuntimeError("Committed order comparison could not be reloaded.")
    return _order_comparison_response(batch, replayed=False)


def import_selling_orders_csv(
    db: Any,
    user: dict[str, Any],
    csv_text: str | bytes,
    *,
    sales_channel: str,
    idempotency_key: str,
    source_filename: str | None = None,
) -> dict[str, Any]:
    if isinstance(csv_text, bytes):
        if len(csv_text) > 2_000_000:
            raise DomainError(
                413,
                "order_file_too_large",
                "Keep the order CSV under 2 MB.",
            )
        try:
            decoded = csv_text.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise DomainError(
                422,
                "invalid_csv_encoding",
                "Upload a UTF-8 CSV file.",
            ) from error
    else:
        decoded = csv_text.lstrip("\ufeff")
        if len(decoded.encode("utf-8")) > 2_000_000:
            raise DomainError(
                413,
                "order_file_too_large",
                "Keep the order CSV under 2 MB.",
            )

    payload, rows = _parse_selling_order_csv(
        decoded,
        sales_channel=sales_channel,
        idempotency_key=idempotency_key,
    )
    return create_order_comparison(
        db,
        user,
        payload,
        source_filename=source_filename,
        source_row_numbers=[row.line_number for row in rows],
    )


def resolve_order_comparison(
    db: Any,
    user: dict[str, Any],
    line_id: str,
    payload: OrderComparisonResolve,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_id = line_id.strip()
    fingerprint = _payload_fingerprint(
        payload,
        exclude={"idempotency_key"},
        actor_user_id=user["id"],
        action=f"order-comparison:resolve:{normalized_id}",
    )
    timestamp = utc_now()

    try:
        _begin_order_comparison_resolution(db)
        existing_by_key = _load_order_comparison_line(
            db,
            tenant,
            resolve_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing_by_key is not None:
            if (
                existing_by_key["id"] != normalized_id
                or existing_by_key.get("resolve_fingerprint") != fingerprint
            ):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another order check.",
                )
            batch = _load_order_comparison(
                db,
                tenant,
                batch_id=existing_by_key["batch_id"],
            )
            if batch is None:
                raise RuntimeError("Resolved order comparison could not be loaded.")
            db.commit()
            return _order_comparison_response(batch, replayed=True)

        line = _load_order_comparison_line(
            db,
            tenant,
            line_id=normalized_id,
            for_update=True,
        )
        if line is None:
            raise DomainError(
                404,
                "order_comparison_not_found",
                "Order comparison line not found.",
            )
        current_batch = _load_order_comparison(
            db,
            tenant,
            batch_id=line["batch_id"],
        )
        if current_batch is None:
            raise RuntimeError("Order comparison batch could not be loaded.")
        current_line = next(
            (
                candidate
                for candidate in current_batch["lines"]
                if candidate["id"] == normalized_id
            ),
            None,
        )
        if current_line is None:
            raise RuntimeError("Order comparison line could not be reloaded.")
        current_state = order_comparison_line_public(current_line)
        if current_state["stale"]:
            raise DomainError(
                409,
                "order_comparison_stale",
                "Stockpile records changed after this check. Import a fresh order file.",
            )
        if line["comparison_status"] == "matched":
            raise DomainError(
                409,
                "order_line_already_matched",
                "This order line already matches Stockpile.",
            )
        if line.get("resolution") is not None:
            raise DomainError(
                409,
                "order_line_already_resolved",
                "This order discrepancy is already resolved.",
            )

        transaction_id: str | None = None
        if payload.resolution == "link_removal":
            if current_state["evidence_issue"] is not None:
                raise DomainError(
                    409,
                    "invalid_order_evidence",
                    "Fix the Stockpile order record before linking another removal.",
                )
            if line["comparison_status"] == "unknown_product" or not line.get(
                "sku"
            ):
                raise DomainError(
                    409,
                    "unknown_order_product",
                    "Add or correct the product before linking a stock removal.",
                )
            deficit = int(line["ordered_quantity"]) - int(
                line["stockpile_quantity"]
            )
            if deficit <= 0:
                raise DomainError(
                    409,
                    "order_has_no_removal_deficit",
                    "This discrepancy cannot be resolved by linking another removal.",
                )
            transaction_id = payload.inventory_transaction_id or ""
            transaction = _order_removal(
                db,
                tenant,
                transaction_id,
            )
            if transaction is None:
                raise DomainError(
                    404,
                    "transaction_not_found",
                    "Stock removal not found.",
                )
            if (
                transaction["movement_type"] != "stock_out"
                or transaction["source"] not in {"scanner", "manual"}
                or transaction.get("reversed_by_transaction_id")
            ):
                raise DomainError(
                    409,
                    "invalid_order_removal",
                    "Choose an unreversed scanner or manual stock removal.",
                )
            if transaction["sku"] != line["sku"]:
                raise DomainError(
                    409,
                    "order_removal_product_mismatch",
                    "The stock removal belongs to another product.",
                )
            if int(transaction["quantity"]) != deficit:
                raise DomainError(
                    409,
                    "order_removal_quantity_mismatch",
                    f"Choose a removal for exactly {deficit} units.",
                )
            reserved_use = fetchone(
                db,
                """
                SELECT reservation_id FROM inventory_reservation_lines
                WHERE tenant_id = ? AND fulfillment_transaction_id = ?
                LIMIT 1
                """,
                (tenant, transaction_id),
                """
                SELECT reservation_id FROM inventory_reservation_lines
                WHERE tenant_id = %s AND fulfillment_transaction_id = %s
                LIMIT 1
                """,
            )
            if reserved_use is not None:
                raise DomainError(
                    409,
                    "order_removal_already_used",
                    "This removal already belongs to a fulfilled Stockpile order.",
                )
            linked_use = fetchone(
                db,
                """
                SELECT id FROM order_comparison_lines
                WHERE tenant_id = ? AND inventory_transaction_id = ?
                LIMIT 1
                """,
                (tenant, transaction_id),
                """
                SELECT id FROM order_comparison_lines
                WHERE tenant_id = %s AND inventory_transaction_id = %s
                LIMIT 1
                """,
            )
            if linked_use is not None:
                raise DomainError(
                    409,
                    "order_removal_already_linked",
                    "This removal is already linked to another order check.",
                )

        rowcount = execute_write(
            db,
            """
            UPDATE order_comparison_lines
            SET resolution = ?, inventory_transaction_id = ?,
                resolution_reason = ?, resolve_idempotency_key = ?,
                resolve_fingerprint = ?, resolved_by_user_id = ?,
                resolved_by_name = ?, resolved_at = ?
            WHERE tenant_id = ? AND id = ?
              AND resolution IS NULL
              AND comparison_status <> 'matched'
            """,
            (
                payload.resolution,
                transaction_id,
                payload.reason,
                payload.idempotency_key,
                fingerprint,
                user["id"],
                user["display_name"],
                timestamp,
                tenant,
                normalized_id,
            ),
            """
            UPDATE order_comparison_lines
            SET resolution = %s, inventory_transaction_id = %s,
                resolution_reason = %s, resolve_idempotency_key = %s,
                resolve_fingerprint = %s, resolved_by_user_id = %s,
                resolved_by_name = %s, resolved_at = %s
            WHERE tenant_id = %s AND id = %s
              AND resolution IS NULL
              AND comparison_status <> 'matched'
            """,
        )
        if rowcount != 1:
            raise DomainError(
                409,
                "order_comparison_conflict",
                "This order discrepancy changed. Review and retry.",
            )
        batch_id = line["batch_id"]
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing_by_key = _load_order_comparison_line(
            db,
            tenant,
            resolve_idempotency_key=payload.idempotency_key,
        )
        if (
            existing_by_key is not None
            and existing_by_key["id"] == normalized_id
            and existing_by_key.get("resolve_fingerprint") == fingerprint
        ):
            batch = _load_order_comparison(
                db,
                tenant,
                batch_id=existing_by_key["batch_id"],
            )
            if batch is None:
                raise RuntimeError("Resolved order comparison could not be loaded.")
            return _order_comparison_response(batch, replayed=True)
        if existing_by_key is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another order check.",
            )
        if payload.inventory_transaction_id:
            linked_use = fetchone(
                db,
                """
                SELECT id FROM order_comparison_lines
                WHERE tenant_id = ? AND inventory_transaction_id = ?
                LIMIT 1
                """,
                (tenant, payload.inventory_transaction_id),
                """
                SELECT id FROM order_comparison_lines
                WHERE tenant_id = %s AND inventory_transaction_id = %s
                LIMIT 1
                """,
            )
            if linked_use is not None:
                raise DomainError(
                    409,
                    "order_removal_already_linked",
                    "This removal is already linked to another order check.",
                )
        raise
    except Exception:
        db.rollback()
        raise

    batch = _load_order_comparison(
        db,
        tenant,
        batch_id=batch_id,
    )
    if batch is None:
        raise RuntimeError("Resolved order comparison could not be reloaded.")
    return _order_comparison_response(batch, replayed=False)


def _load_stock_count(
    db: Any,
    tenant: str,
    *,
    count_id: str | None = None,
    create_idempotency_key: str | None = None,
    close_idempotency_key: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    identifiers = [
        count_id is not None,
        create_idempotency_key is not None,
        close_idempotency_key is not None,
    ]
    if sum(identifiers) != 1:
        raise ValueError("Load a stock count by exactly one identifier.")
    if count_id is not None:
        sqlite_where = "tenant_id = ? AND id = ?"
        postgres_where = "tenant_id = %s AND id = %s"
        value = count_id
    elif create_idempotency_key is not None:
        sqlite_where = "tenant_id = ? AND create_idempotency_key = ?"
        postgres_where = "tenant_id = %s AND create_idempotency_key = %s"
        value = create_idempotency_key
    else:
        sqlite_where = "tenant_id = ? AND close_idempotency_key = ?"
        postgres_where = "tenant_id = %s AND close_idempotency_key = %s"
        value = close_idempotency_key
    count = fetchone(
        db,
        f"SELECT * FROM inventory_stock_counts WHERE {sqlite_where}",
        (tenant, value),
        (
            f"SELECT * FROM inventory_stock_counts WHERE {postgres_where}"
            + (" FOR UPDATE" if for_update else "")
        ),
    )
    if count is None:
        return None
    count["lines"] = fetchall(
        db,
        """
        SELECT * FROM inventory_stock_count_lines
        WHERE tenant_id = ? AND count_id = ?
        ORDER BY product_name, sku
        """,
        (tenant, count["id"]),
        """
        SELECT * FROM inventory_stock_count_lines
        WHERE tenant_id = %s AND count_id = %s
        ORDER BY product_name, sku
        """,
    )
    return count


def stock_count_public(record: dict[str, Any]) -> dict[str, Any]:
    lines = record.get("lines", [])
    return {
        "id": record["id"],
        "location_code": record["location_code"],
        "location_name": record["location_name"],
        "location_path": record["location_path"],
        "status": record["status"],
        "counter_user_id": record["counter_user_id"],
        "counter_name": record["counter_name"],
        "created_by_user_id": record["created_by_user_id"],
        "created_by_name": record["created_by_name"],
        "closed_by_user_id": record.get("closed_by_user_id"),
        "closed_by_name": record.get("closed_by_name"),
        "note": record.get("note"),
        "close_reason": record.get("close_reason"),
        "created_at": record["created_at"],
        "closed_at": record.get("closed_at"),
        "difference": sum(int(line["difference"]) for line in lines),
        "discrepancy_lines": sum(
            1 for line in lines if int(line["difference"]) != 0
        ),
        "lines": [
            {
                "sku": line["sku"],
                "barcode": line["barcode"],
                "product_name": line["product_name"],
                "expected_quantity": int(line["expected_quantity"]),
                "counted_quantity": int(line["counted_quantity"]),
                "difference": int(line["difference"]),
                "inventory_balance_before": int(
                    line["inventory_balance_before"]
                ),
                "inventory_balance_after": line.get(
                    "inventory_balance_after"
                ),
                "location_balance_after": line.get(
                    "location_balance_after"
                ),
                "adjustment_transaction_id": line.get(
                    "adjustment_transaction_id"
                ),
            }
            for line in lines
        ],
    }


def list_stock_counts(
    db: Any,
    tenant: str,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if status not in {None, "pending", "applied", "cancelled"}:
        raise DomainError(
            422,
            "invalid_count_status",
            "Stock count status is invalid.",
        )
    if status is None:
        sqlite_query = (
            "SELECT id FROM inventory_stock_counts WHERE tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        postgres_query = (
            "SELECT id FROM inventory_stock_counts WHERE tenant_id = %s "
            "ORDER BY created_at DESC LIMIT %s"
        )
        params = (tenant, limit)
    else:
        sqlite_query = (
            "SELECT id FROM inventory_stock_counts "
            "WHERE tenant_id = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        postgres_query = (
            "SELECT id FROM inventory_stock_counts "
            "WHERE tenant_id = %s AND status = %s "
            "ORDER BY created_at DESC LIMIT %s"
        )
        params = (tenant, status, limit)
    records: list[dict[str, Any]] = []
    for row in fetchall(
        db,
        sqlite_query,
        params,
        postgres_query,
    ):
        record = _load_stock_count(db, tenant, count_id=row["id"])
        if record is not None:
            records.append(stock_count_public(record))
    return records


def create_stock_count(
    db: Any,
    user: dict[str, Any],
    payload: StockCountCreate,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    count_id = str(uuid.uuid4())
    timestamp = utc_now()
    fingerprint: str | None = None
    try:
        begin_write(db)
        counter = _employee_for_badge(
            db,
            tenant,
            payload.counter_badge_code,
            payload.counter_pin,
            for_update=True,
        )
        fingerprint = _payload_fingerprint(
            payload,
            exclude={
                "idempotency_key",
                "counter_badge_code",
                "counter_pin",
            },
            confirmed_user_id=counter["id"],
        )
        existing = _load_stock_count(
            db,
            tenant,
            create_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another stock count.",
                )
            db.commit()
            return _workflow_response(
                db,
                tenant,
                stock_count_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )

        location = _location_for_write(db, tenant, payload.location_code)
        fetchone(
            db,
            "SELECT code FROM locations WHERE tenant_id = ? AND code = ?",
            (tenant, location["code"]),
            """
            SELECT code FROM locations
            WHERE tenant_id = %s AND code = %s FOR UPDATE
            """,
        )
        pending = fetchone(
            db,
            """
            SELECT id FROM inventory_stock_counts
            WHERE tenant_id = ? AND location_code = ? AND status = 'pending'
            LIMIT 1
            """,
            (tenant, location["code"]),
            """
            SELECT id FROM inventory_stock_counts
            WHERE tenant_id = %s AND location_code = %s
              AND status = 'pending'
            LIMIT 1
            """,
        )
        if pending is not None:
            raise DomainError(
                409,
                "count_already_open",
                "Finish or cancel the open count for this location first.",
            )

        execute_write(
            db,
            """
            INSERT INTO inventory_stock_counts
                (id, tenant_id, create_idempotency_key,
                 request_fingerprint, location_code, location_name,
                 location_path, status, counter_user_id, counter_name,
                 created_by_user_id, created_by_name, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
            """,
            (
                count_id,
                tenant,
                payload.idempotency_key,
                fingerprint,
                location["code"],
                location["name"],
                location["path"],
                counter["id"],
                counter["display_name"],
                user["id"],
                user["display_name"],
                payload.note,
                timestamp,
            ),
            """
            INSERT INTO inventory_stock_counts
                (id, tenant_id, create_idempotency_key,
                 request_fingerprint, location_code, location_name,
                 location_path, status, counter_user_id, counter_name,
                 created_by_user_id, created_by_name, note, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s)
            """,
        )

        for line in sorted(
            payload.lines,
            key=lambda entry: entry.barcode,
        ):
            item = _item_for_write(db, tenant, line.barcode)
            if item is None:
                raise DomainError(
                    404,
                    "unknown_barcode",
                    f"No product matches barcode {line.barcode}.",
                )
            position = _position_for_write(
                db,
                tenant,
                item["sku"],
                location["code"],
                timestamp,
            )
            _assert_position_total(
                db,
                tenant,
                item["sku"],
                item["current_stock"],
            )
            expected = int(position["quantity"])
            execute_write(
                db,
                """
                INSERT INTO inventory_stock_count_lines
                    (tenant_id, count_id, sku, barcode, product_name,
                     expected_quantity, counted_quantity, difference,
                     inventory_balance_before)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant,
                    count_id,
                    item["sku"],
                    item["barcode"],
                    item["product_name"],
                    expected,
                    line.quantity,
                    line.quantity - expected,
                    item["current_stock"],
                ),
                """
                INSERT INTO inventory_stock_count_lines
                    (tenant_id, count_id, sku, barcode, product_name,
                     expected_quantity, counted_quantity, difference,
                     inventory_balance_before)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_stock_count(
            db,
            tenant,
            create_idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and fingerprint is not None
            and existing["request_fingerprint"] == fingerprint
        ):
            return _workflow_response(
                db,
                tenant,
                stock_count_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another stock count.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    count = _load_stock_count(db, tenant, count_id=count_id)
    if count is None:
        raise RuntimeError("Committed stock count could not be reloaded.")
    return _workflow_response(
        db,
        tenant,
        stock_count_public(count),
        replayed=False,
        skus={line["sku"] for line in count["lines"]},
    )


def approve_stock_count(
    db: Any,
    user: dict[str, Any],
    count_id: str,
    payload: StockCountApprove,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_id = count_id.strip()
    timestamp = utc_now()
    fingerprint = _payload_fingerprint(
        payload,
        exclude={"idempotency_key"},
        confirmed_user_id=user["id"],
        action="approve",
    )
    try:
        begin_write(db)
        existing_by_key = _load_stock_count(
            db,
            tenant,
            close_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing_by_key is not None:
            if (
                existing_by_key["id"] != normalized_id
                or existing_by_key.get("close_fingerprint") != fingerprint
                or existing_by_key["status"] != "applied"
            ):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another count action.",
                )
            db.commit()
            return _workflow_response(
                db,
                tenant,
                stock_count_public(existing_by_key),
                replayed=True,
                skus={line["sku"] for line in existing_by_key["lines"]},
            )

        count = _load_stock_count(
            db,
            tenant,
            count_id=normalized_id,
            for_update=True,
        )
        if count is None:
            raise DomainError(404, "count_not_found", "Stock count not found.")
        if count["status"] != "pending":
            raise DomainError(
                409,
                "count_already_closed",
                "This stock count is already closed.",
            )
        location = _location_for_write(
            db,
            tenant,
            count["location_code"],
            require_active_stockable=False,
        )

        for line in sorted(count["lines"], key=lambda entry: entry["sku"]):
            item = _item_by_sku_for_write(db, tenant, line["sku"])
            if item is None:
                raise DomainError(
                    409,
                    "missing_inventory",
                    f"{line['product_name']} no longer exists.",
                )
            position = _position_for_write(
                db,
                tenant,
                item["sku"],
                location["code"],
                timestamp,
            )
            if int(position["quantity"]) != int(line["expected_quantity"]):
                raise DomainError(
                    409,
                    "count_stale",
                    f"{item['product_name']} changed after it was counted. "
                    "Run this count again.",
                )
            difference = int(line["difference"])
            transaction_id: str | None = None
            if difference:
                transaction_id = _apply_stock_delta(
                    db,
                    tenant=tenant,
                    user=user,
                    item=item,
                    location=location,
                    quantity_delta=difference,
                    idempotency_key=_workflow_transaction_key(
                        "count",
                        normalized_id,
                        item["sku"],
                    ),
                    reason=payload.reason,
                    source="count",
                    timestamp=timestamp,
                    exchange_status="not_configured",
                )
                transaction = _load_transaction(
                    db,
                    tenant,
                    transaction_id=transaction_id,
                )
                if transaction is None:
                    raise RuntimeError("Count transaction could not be loaded.")
                inventory_after = int(transaction["balance_after"])
                location_after = int(transaction["location_balance_after"])
            else:
                inventory_after = int(item["current_stock"])
                location_after = int(position["quantity"])

            execute_write(
                db,
                """
                UPDATE inventory_stock_count_lines
                SET inventory_balance_after = ?,
                    location_balance_after = ?,
                    adjustment_transaction_id = ?
                WHERE tenant_id = ? AND count_id = ? AND sku = ?
                """,
                (
                    inventory_after,
                    location_after,
                    transaction_id,
                    tenant,
                    normalized_id,
                    item["sku"],
                ),
                """
                UPDATE inventory_stock_count_lines
                SET inventory_balance_after = %s,
                    location_balance_after = %s,
                    adjustment_transaction_id = %s
                WHERE tenant_id = %s AND count_id = %s AND sku = %s
                """,
            )

        rowcount = execute_write(
            db,
            """
            UPDATE inventory_stock_counts
            SET status = 'applied', close_idempotency_key = ?,
                close_fingerprint = ?, closed_by_user_id = ?,
                closed_by_name = ?, close_reason = ?, closed_at = ?
            WHERE tenant_id = ? AND id = ? AND status = 'pending'
            """,
            (
                payload.idempotency_key,
                fingerprint,
                user["id"],
                user["display_name"],
                payload.reason,
                timestamp,
                tenant,
                normalized_id,
            ),
            """
            UPDATE inventory_stock_counts
            SET status = 'applied', close_idempotency_key = %s,
                close_fingerprint = %s, closed_by_user_id = %s,
                closed_by_name = %s, close_reason = %s, closed_at = %s
            WHERE tenant_id = %s AND id = %s AND status = 'pending'
            """,
        )
        if rowcount != 1:
            raise DomainError(
                409,
                "count_conflict",
                "This stock count changed. Review and retry.",
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_stock_count(
            db,
            tenant,
            close_idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and existing["id"] == normalized_id
            and existing.get("close_fingerprint") == fingerprint
            and existing["status"] == "applied"
        ):
            return _workflow_response(
                db,
                tenant,
                stock_count_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another count action.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    applied = _load_stock_count(db, tenant, count_id=normalized_id)
    if applied is None:
        raise RuntimeError("Applied stock count could not be reloaded.")
    return _workflow_response(
        db,
        tenant,
        stock_count_public(applied),
        replayed=False,
        skus={line["sku"] for line in applied["lines"]},
    )


def cancel_stock_count(
    db: Any,
    user: dict[str, Any],
    count_id: str,
    payload: StockCountCancel,
) -> dict[str, Any]:
    tenant = user["tenant_id"]
    normalized_id = count_id.strip()
    timestamp = utc_now()
    fingerprint = _payload_fingerprint(
        payload,
        exclude={"idempotency_key"},
        confirmed_user_id=user["id"],
        action="cancel",
    )
    try:
        begin_write(db)
        existing_by_key = _load_stock_count(
            db,
            tenant,
            close_idempotency_key=payload.idempotency_key,
            for_update=True,
        )
        if existing_by_key is not None:
            if (
                existing_by_key["id"] != normalized_id
                or existing_by_key.get("close_fingerprint") != fingerprint
                or existing_by_key["status"] != "cancelled"
            ):
                raise DomainError(
                    409,
                    "idempotency_conflict",
                    "This submission key was already used for another count action.",
                )
            db.commit()
            return _workflow_response(
                db,
                tenant,
                stock_count_public(existing_by_key),
                replayed=True,
                skus={line["sku"] for line in existing_by_key["lines"]},
            )

        count = _load_stock_count(
            db,
            tenant,
            count_id=normalized_id,
            for_update=True,
        )
        if count is None:
            raise DomainError(404, "count_not_found", "Stock count not found.")
        if count["status"] != "pending":
            raise DomainError(
                409,
                "count_already_closed",
                "This stock count is already closed.",
            )
        rowcount = execute_write(
            db,
            """
            UPDATE inventory_stock_counts
            SET status = 'cancelled', close_idempotency_key = ?,
                close_fingerprint = ?, closed_by_user_id = ?,
                closed_by_name = ?, close_reason = ?, closed_at = ?
            WHERE tenant_id = ? AND id = ? AND status = 'pending'
            """,
            (
                payload.idempotency_key,
                fingerprint,
                user["id"],
                user["display_name"],
                payload.reason,
                timestamp,
                tenant,
                normalized_id,
            ),
            """
            UPDATE inventory_stock_counts
            SET status = 'cancelled', close_idempotency_key = %s,
                close_fingerprint = %s, closed_by_user_id = %s,
                closed_by_name = %s, close_reason = %s, closed_at = %s
            WHERE tenant_id = %s AND id = %s AND status = 'pending'
            """,
        )
        if rowcount != 1:
            raise DomainError(
                409,
                "count_conflict",
                "This stock count changed. Review and retry.",
            )
        db.commit()
    except INTEGRITY_ERRORS:
        db.rollback()
        existing = _load_stock_count(
            db,
            tenant,
            close_idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and existing["id"] == normalized_id
            and existing.get("close_fingerprint") == fingerprint
            and existing["status"] == "cancelled"
        ):
            return _workflow_response(
                db,
                tenant,
                stock_count_public(existing),
                replayed=True,
                skus={line["sku"] for line in existing["lines"]},
            )
        if existing is not None:
            raise DomainError(
                409,
                "idempotency_conflict",
                "This submission key was already used for another count action.",
            )
        raise
    except Exception:
        db.rollback()
        raise

    cancelled = _load_stock_count(db, tenant, count_id=normalized_id)
    if cancelled is None:
        raise RuntimeError("Cancelled stock count could not be reloaded.")
    return _workflow_response(
        db,
        tenant,
        stock_count_public(cancelled),
        replayed=False,
        skus={line["sku"] for line in cancelled["lines"]},
    )
