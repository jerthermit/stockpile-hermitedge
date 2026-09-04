import hashlib
import hmac
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor

from config import database_file, database_url, seed_demo_data, tenant_id
from security import hash_badge_pin, hash_password


logger = logging.getLogger("stockpile.database")

DEMO_INVENTORY = (
    ("PKG-BAG-M", "2908170000012", "Clear Poly Bag 12 × 18 in · Pack of 100", "Packing & Labels", 240),
    ("BOX-060", "2908170000029", "Textile Dispatch Carton 60 cm", "Packing & Labels", 84),
    ("TAPE-048", "2908170000036", "Carton Sealing Tape 48 mm · Pack of 6", "Packing & Labels", 160),
    ("LBL-040", "2908170000043", "Thermal Roll Label 40 mm · 1,000 labels", "Packing & Labels", 42),
    ("TAG-100", "2908170000050", "Fabric Roll Tags · Pack of 100", "Packing & Labels", 96),
    ("BAG-XL", "2908170000067", "Clear Garment Bag 24 × 40 in · Pack of 50", "Packing & Labels", 120),
    ("TXT-POP-WHT-60", "2908170000074", "Cotton Poplin 60 in · White · 50 m roll", "Cotton Wovens", 38),
    ("TXT-POP-BLK-60", "2908170000081", "Cotton Poplin 60 in · Black · 50 m roll", "Cotton Wovens", 31),
    ("TXT-BC-WHT-60", "2908170000098", "Cotton Broadcloth 60 in · White · 50 m roll", "Cotton Wovens", 24),
    ("TXT-TWL-KHK-60", "2908170000104", "Cotton Twill 60 in · Khaki · 50 m roll", "Cotton Wovens", 17),
    ("TXT-TWL-NVY-60", "2908170000111", "Cotton Twill 60 in · Navy · 50 m roll", "Cotton Wovens", 14),
    ("TXT-CNV-NAT-60", "2908170000128", "Cotton Canvas 60 in · Natural · 50 m roll", "Cotton Wovens", 12),
    ("TXT-MUS-UNB-45", "2908170000135", "Unbleached Muslin 45 in · 50 m roll", "Cotton Wovens", 29),
    ("TXT-GNG-RBW-45", "2908170000142", "Gingham 45 in · Red/White · 50 m roll", "Cotton Wovens", 8),
    ("TXT-OXF-WHT-60", "2908170000159", "Oxford Shirting 60 in · White · 50 m roll", "Uniform & Shirting", 33),
    ("TXT-OXF-BLU-60", "2908170000166", "Oxford Shirting 60 in · Sky Blue · 50 m roll", "Uniform & Shirting", 27),
    ("TXT-TC-WHT-60", "2908170000173", "TC Shirting 60 in · White · 50 m roll", "Uniform & Shirting", 44),
    ("TXT-GAB-NVY-60", "2908170000180", "Gabardine 60 in · Navy · 50 m roll", "Uniform & Shirting", 19),
    ("TXT-GAB-BLK-60", "2908170000197", "Gabardine 60 in · Black · 50 m roll", "Uniform & Shirting", 22),
    ("TXT-PTW-KHK-60", "2908170000203", "Peach Twill 60 in · Khaki · 50 m roll", "Uniform & Shirting", 11),
    ("TXT-JRS-BLK-60", "2908170000210", "Single Jersey 60 in · Black · 25 kg roll", "Knits & Stretch", 16),
    ("TXT-JRS-WHT-60", "2908170000227", "Single Jersey 60 in · White · 25 kg roll", "Knits & Stretch", 18),
    ("TXT-RIB-GRY-45", "2908170000234", "Rib Knit 45 in · Heather Gray · 20 kg roll", "Knits & Stretch", 9),
    ("TXT-SPX-BLK-60", "2908170000241", "Cotton Spandex 60 in · Black · 25 kg roll", "Knits & Stretch", 13),
    ("TXT-FTR-GRY-60", "2908170000258", "French Terry 60 in · Gray · 25 kg roll", "Knits & Stretch", 7),
    ("TXT-MSH-WHT-60", "2908170000265", "Polyester Mesh 60 in · White · 50 m roll", "Knits & Stretch", 21),
    ("TXT-SAT-CHP-60", "2908170000272", "Bridal Satin 60 in · Champagne · 50 m roll", "Formal & Occasion", 15),
    ("TXT-SAT-BLK-60", "2908170000289", "Bridal Satin 60 in · Black · 50 m roll", "Formal & Occasion", 10),
    ("TXT-CHF-WHT-60", "2908170000296", "Chiffon 60 in · White · 50 m roll", "Formal & Occasion", 23),
    ("TXT-ORG-IVO-60", "2908170000302", "Organza 60 in · Ivory · 50 m roll", "Formal & Occasion", 6),
    ("TXT-TUL-WHT-72", "2908170000319", "Soft Tulle 72 in · White · 50 m roll", "Formal & Occasion", 20),
    ("TXT-LCE-IVO-60", "2908170000326", "Floral Lace 60 in · Ivory · 30 m roll", "Formal & Occasion", 0),
    ("TXT-LIN-BLK-60", "2908170000333", "Polyester Lining 60 in · Black · 50 m roll", "Lining & Interfacing", 34),
    ("TXT-LIN-WHT-60", "2908170000340", "Polyester Lining 60 in · White · 50 m roll", "Lining & Interfacing", 26),
    ("TXT-LIN-BGE-60", "2908170000357", "Polyester Lining 60 in · Beige · 50 m roll", "Lining & Interfacing", 18),
    ("TXT-FUS-LGT-60", "2908170000364", "Fusible Interfacing 60 in · Light · 50 m roll", "Lining & Interfacing", 14),
    ("TXT-FUS-MED-60", "2908170000371", "Fusible Interfacing 60 in · Medium · 50 m roll", "Lining & Interfacing", 12),
    ("TXT-INT-NWV-60", "2908170000388", "Nonwoven Interlining 60 in · White · 100 m roll", "Lining & Interfacing", 9),
    ("NOT-THR-WHT-12", "2908170000395", "Polyester Thread · White · Box of 12 cones", "Notions", 52),
    ("NOT-THR-BLK-12", "2908170000401", "Polyester Thread · Black · Box of 12 cones", "Notions", 47),
)

LEGACY_DEMO_INVENTORY = (
    ("PKG-BAG-M", "4800000000011", "Resealable Bags, Medium", "Packaging"),
    ("BOX-060", "4800000000028", "Shipping Box 60 cm", "Packaging"),
    ("TAPE-048", "4800000000035", "Packing Tape 48 mm", "Supplies"),
    ("LBL-040", "4800000000042", "Thermal Label Roll 40 mm", "Labels"),
    ("TAG-100", "4800000000059", "Price Tags, Pack of 100", "Labels"),
    ("BAG-XL", "4800000000066", "Carry Bags, Extra Large", "Packaging"),
)

DEMO_INVENTORY_BY_SKU = {item[0]: item for item in DEMO_INVENTORY}

UNASSIGNED_LOCATION = (
    "UNASSIGNED",
    "Location not recorded",
    "holding",
    None,
    True,
)

DEMO_LOCATIONS = (
    ("WH-MNL", "Warehouse 01", "warehouse", None, False),
    ("WH-MNL-REC", "Receiving Bay", "receiving", "WH-MNL", True),
    ("WH-MNL-TXT", "Fabric Storage", "zone", "WH-MNL", False),
    ("WH-MNL-A01", "Rack A · Bay 01", "rack", "WH-MNL-TXT", True),
    ("WH-MNL-A02", "Rack A · Bay 02", "rack", "WH-MNL-TXT", True),
    ("WH-MNL-B01", "Rack B · Bay 01", "rack", "WH-MNL-TXT", True),
    ("WH-MNL-B02", "Rack B · Bay 02", "rack", "WH-MNL-TXT", True),
    ("WH-MNL-C01", "Rack C · Bay 01", "rack", "WH-MNL-TXT", True),
    ("WH-MNL-C02", "Rack C · Bay 02", "rack", "WH-MNL-TXT", True),
    ("WH-MNL-SUP", "Packing & Notions", "zone", "WH-MNL", False),
    ("WH-MNL-D01", "Rack D · Shelf 01 · Bin 01", "bin", "WH-MNL-SUP", True),
    ("WH-MNL-D02", "Rack D · Shelf 01 · Bin 02", "bin", "WH-MNL-SUP", True),
)

DEMO_CATEGORY_LOCATIONS = {
    "Packing & Labels": ("WH-MNL-D01", "WH-MNL-D02"),
    "Cotton Wovens": ("WH-MNL-A01", "WH-MNL-A02"),
    "Uniform & Shirting": ("WH-MNL-A02", "WH-MNL-B01"),
    "Knits & Stretch": ("WH-MNL-B01", "WH-MNL-B02"),
    "Formal & Occasion": ("WH-MNL-B02", "WH-MNL-C01"),
    "Lining & Interfacing": ("WH-MNL-C01", "WH-MNL-C02"),
    "Notions": ("WH-MNL-D01", "WH-MNL-D02"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def badge_code_digest(value: str, tenant: str | None = None) -> str:
    normalized = value.strip().upper()
    badge_secret = os.getenv("STOCKPILE_BADGE_SECRET", "").strip()
    key = (badge_secret or f"stockpile-badge:{tenant or tenant_id()}").encode(
        "utf-8"
    )
    return hmac.new(
        key,
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def is_sqlite(db: Any) -> bool:
    return isinstance(db, sqlite3.Connection)


def get_db() -> Iterator[Any]:
    url = database_url()
    if url:
        connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    else:
        path = database_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")

    try:
        yield connection
    finally:
        connection.close()


def execute(
    db: Any,
    sqlite_query: str,
    params: Sequence[Any] = (),
    postgres_query: str | None = None,
):
    query = sqlite_query if is_sqlite(db) else (postgres_query or sqlite_query)
    if is_sqlite(db):
        return db.execute(query, tuple(params))
    cursor = db.cursor()
    cursor.execute(query, tuple(params))
    return cursor


def execute_write(
    db: Any,
    sqlite_query: str,
    params: Sequence[Any] = (),
    postgres_query: str | None = None,
) -> int:
    cursor = execute(db, sqlite_query, params, postgres_query)
    rowcount = cursor.rowcount
    cursor.close()
    return rowcount


def fetchone(
    db: Any,
    sqlite_query: str,
    params: Sequence[Any] = (),
    postgres_query: str | None = None,
) -> dict[str, Any] | None:
    cursor = execute(db, sqlite_query, params, postgres_query)
    row = cursor.fetchone()
    cursor.close()
    return dict(row) if row else None


def fetchall(
    db: Any,
    sqlite_query: str,
    params: Sequence[Any] = (),
    postgres_query: str | None = None,
) -> list[dict[str, Any]]:
    cursor = execute(db, sqlite_query, params, postgres_query)
    rows = cursor.fetchall()
    cursor.close()
    return [dict(row) for row in rows]


def begin_write(db: Any) -> None:
    if is_sqlite(db):
        db.execute("BEGIN IMMEDIATE")


def init_db() -> None:
    if database_url():
        _init_postgres()
    else:
        _init_sqlite()


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _init_stock_control_tables(db: Any) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS inventory_receipts (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            idempotency_key VARCHAR NOT NULL,
            request_fingerprint VARCHAR NOT NULL,
            supplier_name VARCHAR,
            reference VARCHAR,
            source VARCHAR NOT NULL CHECK (
                source IN ('scanner', 'document', 'manual', 'import')
            ),
            status VARCHAR NOT NULL DEFAULT 'received'
                CHECK (status = 'received'),
            receiver_user_id VARCHAR NOT NULL,
            receiver_name VARCHAR NOT NULL,
            created_by_user_id VARCHAR NOT NULL,
            created_by_name VARCHAR NOT NULL,
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            received_at TIMESTAMPTZ NOT NULL,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, idempotency_key),
            FOREIGN KEY (tenant_id, receiver_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, created_by_user_id)
                REFERENCES users(tenant_id, id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_receipt_lines (
            tenant_id VARCHAR NOT NULL,
            receipt_id VARCHAR NOT NULL,
            sku VARCHAR NOT NULL,
            barcode VARCHAR NOT NULL,
            product_name VARCHAR NOT NULL,
            location_code VARCHAR NOT NULL,
            location_name VARCHAR NOT NULL,
            location_path VARCHAR NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            inventory_balance_before INTEGER NOT NULL
                CHECK (inventory_balance_before >= 0),
            inventory_balance_after INTEGER NOT NULL
                CHECK (inventory_balance_after >= 0),
            location_balance_before INTEGER NOT NULL
                CHECK (location_balance_before >= 0),
            location_balance_after INTEGER NOT NULL
                CHECK (location_balance_after >= 0),
            transaction_id VARCHAR NOT NULL,
            PRIMARY KEY (tenant_id, receipt_id, sku, location_code),
            FOREIGN KEY (tenant_id, receipt_id)
                REFERENCES inventory_receipts(tenant_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, sku)
                REFERENCES inventory(tenant_id, sku),
            FOREIGN KEY (tenant_id, location_code)
                REFERENCES locations(tenant_id, code),
            FOREIGN KEY (transaction_id)
                REFERENCES inventory_transactions(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_receipts_tenant_created
        ON inventory_receipts(tenant_id, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_receipt_lines_tenant_sku_location
        ON inventory_receipt_lines(tenant_id, sku, location_code)
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_returns (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            idempotency_key VARCHAR NOT NULL,
            request_fingerprint VARCHAR NOT NULL,
            return_type VARCHAR NOT NULL CHECK (
                return_type IN ('customer_return', 'supplier_return')
            ),
            reference VARCHAR,
            party_name VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'completed'
                CHECK (status = 'completed'),
            employee_user_id VARCHAR NOT NULL,
            employee_name VARCHAR NOT NULL,
            created_by_user_id VARCHAR NOT NULL,
            created_by_name VARCHAR NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ NOT NULL,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, idempotency_key),
            FOREIGN KEY (tenant_id, employee_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, created_by_user_id)
                REFERENCES users(tenant_id, id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_return_lines (
            tenant_id VARCHAR NOT NULL,
            return_id VARCHAR NOT NULL,
            sku VARCHAR NOT NULL,
            barcode VARCHAR NOT NULL,
            product_name VARCHAR NOT NULL,
            location_code VARCHAR NOT NULL,
            location_name VARCHAR NOT NULL,
            location_path VARCHAR NOT NULL,
            condition VARCHAR NOT NULL CHECK (
                condition IN ('sellable', 'damaged')
            ),
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            inventory_balance_before INTEGER NOT NULL
                CHECK (inventory_balance_before >= 0),
            inventory_balance_after INTEGER NOT NULL
                CHECK (inventory_balance_after >= 0),
            location_balance_before INTEGER NOT NULL
                CHECK (location_balance_before >= 0),
            location_balance_after INTEGER NOT NULL
                CHECK (location_balance_after >= 0),
            transaction_id VARCHAR NOT NULL,
            damage_id VARCHAR,
            PRIMARY KEY (
                tenant_id,
                return_id,
                sku,
                location_code,
                condition
            ),
            FOREIGN KEY (tenant_id, return_id)
                REFERENCES inventory_returns(tenant_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, sku)
                REFERENCES inventory(tenant_id, sku),
            FOREIGN KEY (tenant_id, location_code)
                REFERENCES locations(tenant_id, code),
            FOREIGN KEY (transaction_id)
                REFERENCES inventory_transactions(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_returns_tenant_created
        ON inventory_returns(tenant_id, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_return_lines_tenant_sku_location
        ON inventory_return_lines(tenant_id, sku, location_code)
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_damage (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            create_idempotency_key VARCHAR NOT NULL,
            resolve_idempotency_key VARCHAR,
            request_fingerprint VARCHAR NOT NULL,
            resolve_fingerprint VARCHAR,
            sku VARCHAR NOT NULL,
            barcode VARCHAR NOT NULL,
            product_name VARCHAR NOT NULL,
            location_code VARCHAR NOT NULL,
            location_name VARCHAR NOT NULL,
            location_path VARCHAR NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            remaining_quantity INTEGER NOT NULL CHECK (
                remaining_quantity >= 0
                AND remaining_quantity <= quantity
            ),
            category VARCHAR NOT NULL CHECK (
                category IN (
                    'physical_damage',
                    'quality_issue',
                    'expired',
                    'water_damage',
                    'other'
                )
            ),
            reference VARCHAR,
            reason TEXT NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'resolved')),
            resolution VARCHAR CHECK (
                resolution IS NULL OR resolution IN (
                    'restock',
                    'write_off',
                    'return_to_supplier'
                )
            ),
            resolution_reason TEXT,
            source_type VARCHAR,
            source_id VARCHAR,
            reported_by_user_id VARCHAR NOT NULL,
            reported_by_name VARCHAR NOT NULL,
            created_by_user_id VARCHAR NOT NULL,
            created_by_name VARCHAR NOT NULL,
            resolved_by_user_id VARCHAR,
            resolved_by_name VARCHAR,
            resolved_actor_user_id VARCHAR,
            resolved_actor_name VARCHAR,
            inventory_balance_at_report INTEGER NOT NULL
                CHECK (inventory_balance_at_report >= 0),
            location_balance_at_report INTEGER NOT NULL
                CHECK (location_balance_at_report >= 0),
            inventory_balance_after_resolution INTEGER
                CHECK (inventory_balance_after_resolution >= 0),
            location_balance_after_resolution INTEGER
                CHECK (location_balance_after_resolution >= 0),
            resolution_transaction_id VARCHAR,
            created_at TIMESTAMPTZ NOT NULL,
            resolved_at TIMESTAMPTZ,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, create_idempotency_key),
            FOREIGN KEY (tenant_id, sku)
                REFERENCES inventory(tenant_id, sku),
            FOREIGN KEY (tenant_id, location_code)
                REFERENCES locations(tenant_id, code),
            FOREIGN KEY (tenant_id, reported_by_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, created_by_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, resolved_by_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, resolved_actor_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (resolution_transaction_id)
                REFERENCES inventory_transactions(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_damage_tenant_status_location
        ON inventory_damage(tenant_id, status, location_code, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_damage_tenant_sku_location
        ON inventory_damage(tenant_id, sku, location_code, status)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_damage_tenant_resolve_key
        ON inventory_damage(tenant_id, resolve_idempotency_key)
        WHERE resolve_idempotency_key IS NOT NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_reservations (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            create_idempotency_key VARCHAR NOT NULL,
            close_idempotency_key VARCHAR,
            request_fingerprint VARCHAR NOT NULL,
            close_fingerprint VARCHAR,
            reference VARCHAR NOT NULL,
            customer_name VARCHAR,
            sales_channel VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'active' CHECK (
                status IN ('active', 'released', 'fulfilled')
            ),
            note TEXT,
            close_action VARCHAR CHECK (
                close_action IS NULL
                OR close_action IN ('release', 'fulfill')
            ),
            close_reason TEXT,
            created_by_user_id VARCHAR NOT NULL,
            created_by_name VARCHAR NOT NULL,
            closed_by_user_id VARCHAR,
            closed_by_name VARCHAR,
            closed_actor_user_id VARCHAR,
            closed_actor_name VARCHAR,
            created_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, create_idempotency_key),
            FOREIGN KEY (tenant_id, created_by_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, closed_by_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, closed_actor_user_id)
                REFERENCES users(tenant_id, id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_reservation_lines (
            tenant_id VARCHAR NOT NULL,
            reservation_id VARCHAR NOT NULL,
            sku VARCHAR NOT NULL,
            barcode VARCHAR NOT NULL,
            product_name VARCHAR NOT NULL,
            location_code VARCHAR NOT NULL,
            location_name VARCHAR NOT NULL,
            location_path VARCHAR NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            on_hand_at_create INTEGER NOT NULL
                CHECK (on_hand_at_create >= 0),
            reserved_before INTEGER NOT NULL
                CHECK (reserved_before >= 0),
            reserved_after INTEGER NOT NULL
                CHECK (reserved_after >= 0),
            available_before INTEGER NOT NULL
                CHECK (available_before >= 0),
            available_after INTEGER NOT NULL
                CHECK (available_after >= 0),
            inventory_balance_after_fulfillment INTEGER
                CHECK (inventory_balance_after_fulfillment >= 0),
            location_balance_after_fulfillment INTEGER
                CHECK (location_balance_after_fulfillment >= 0),
            fulfillment_transaction_id VARCHAR,
            PRIMARY KEY (tenant_id, reservation_id, sku, location_code),
            FOREIGN KEY (tenant_id, reservation_id)
                REFERENCES inventory_reservations(tenant_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, sku)
                REFERENCES inventory(tenant_id, sku),
            FOREIGN KEY (tenant_id, location_code)
                REFERENCES locations(tenant_id, code),
            FOREIGN KEY (fulfillment_transaction_id)
                REFERENCES inventory_transactions(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_tenant_status_created
        ON inventory_reservations(tenant_id, status, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_tenant_reference
        ON inventory_reservations(tenant_id, reference, created_at DESC)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_reservations_tenant_close_key
        ON inventory_reservations(tenant_id, close_idempotency_key)
        WHERE close_idempotency_key IS NOT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_reservation_lines_tenant_stock
        ON inventory_reservation_lines(tenant_id, sku, location_code)
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_stock_counts (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            create_idempotency_key VARCHAR NOT NULL,
            close_idempotency_key VARCHAR,
            request_fingerprint VARCHAR NOT NULL,
            close_fingerprint VARCHAR,
            location_code VARCHAR NOT NULL,
            location_name VARCHAR NOT NULL,
            location_path VARCHAR NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'applied', 'cancelled')
            ),
            counter_user_id VARCHAR NOT NULL,
            counter_name VARCHAR NOT NULL,
            created_by_user_id VARCHAR NOT NULL,
            created_by_name VARCHAR NOT NULL,
            closed_by_user_id VARCHAR,
            closed_by_name VARCHAR,
            note TEXT,
            close_reason TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, create_idempotency_key),
            FOREIGN KEY (tenant_id, location_code)
                REFERENCES locations(tenant_id, code),
            FOREIGN KEY (tenant_id, counter_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, created_by_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, closed_by_user_id)
                REFERENCES users(tenant_id, id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_stock_count_lines (
            tenant_id VARCHAR NOT NULL,
            count_id VARCHAR NOT NULL,
            sku VARCHAR NOT NULL,
            barcode VARCHAR NOT NULL,
            product_name VARCHAR NOT NULL,
            expected_quantity INTEGER NOT NULL
                CHECK (expected_quantity >= 0),
            counted_quantity INTEGER NOT NULL
                CHECK (counted_quantity >= 0),
            difference INTEGER NOT NULL,
            inventory_balance_before INTEGER NOT NULL
                CHECK (inventory_balance_before >= 0),
            inventory_balance_after INTEGER
                CHECK (inventory_balance_after >= 0),
            location_balance_after INTEGER
                CHECK (location_balance_after >= 0),
            adjustment_transaction_id VARCHAR,
            PRIMARY KEY (tenant_id, count_id, sku),
            FOREIGN KEY (tenant_id, count_id)
                REFERENCES inventory_stock_counts(tenant_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, sku)
                REFERENCES inventory(tenant_id, sku),
            FOREIGN KEY (adjustment_transaction_id)
                REFERENCES inventory_transactions(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_stock_counts_tenant_status_created
        ON inventory_stock_counts(tenant_id, status, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_stock_counts_tenant_location_created
        ON inventory_stock_counts(tenant_id, location_code, created_at DESC)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_stock_counts_tenant_close_key
        ON inventory_stock_counts(tenant_id, close_idempotency_key)
        WHERE close_idempotency_key IS NOT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_stock_count_lines_tenant_sku
        ON inventory_stock_count_lines(tenant_id, sku, count_id)
        """,
    )

    for statement in statements:
        execute_write(db, statement)


def _init_investigation_tables(db: Any) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS inventory_investigations (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            create_idempotency_key VARCHAR NOT NULL,
            resolve_idempotency_key VARCHAR,
            request_fingerprint VARCHAR NOT NULL,
            resolve_fingerprint VARCHAR,
            kind VARCHAR NOT NULL CHECK (
                kind IN ('missing', 'misplaced', 'unexpected')
            ),
            status VARCHAR NOT NULL DEFAULT 'open' CHECK (
                status IN ('open', 'resolved')
            ),
            sku VARCHAR NOT NULL,
            barcode VARCHAR NOT NULL,
            product_name VARCHAR NOT NULL,
            quantity INTEGER NOT NULL CHECK (
                quantity > 0 AND quantity <= 1000000
            ),
            expected_location_code VARCHAR,
            expected_location_name VARCHAR,
            expected_location_path VARCHAR,
            observed_location_code VARCHAR,
            observed_location_name VARCHAR,
            observed_location_path VARCHAR,
            reference VARCHAR,
            note TEXT,
            inventory_balance_at_open INTEGER NOT NULL CHECK (
                inventory_balance_at_open >= 0
            ),
            inventory_version_at_open INTEGER NOT NULL CHECK (
                inventory_version_at_open >= 0
            ),
            expected_location_balance_at_open INTEGER CHECK (
                expected_location_balance_at_open >= 0
            ),
            expected_location_version_at_open INTEGER CHECK (
                expected_location_version_at_open >= 0
            ),
            observed_location_balance_at_open INTEGER CHECK (
                observed_location_balance_at_open >= 0
            ),
            observed_location_version_at_open INTEGER CHECK (
                observed_location_version_at_open >= 0
            ),
            opened_employee_user_id VARCHAR NOT NULL,
            opened_employee_name VARCHAR NOT NULL,
            opened_actor_user_id VARCHAR NOT NULL,
            opened_actor_name VARCHAR NOT NULL,
            resolution VARCHAR CHECK (
                resolution IS NULL OR resolution IN (
                    'located',
                    'confirmed_missing',
                    'record_error',
                    'dismissed'
                )
            ),
            resolution_reference VARCHAR,
            resolution_reason TEXT,
            resolved_location_code VARCHAR,
            resolved_location_name VARCHAR,
            resolved_location_path VARCHAR,
            resolved_employee_user_id VARCHAR,
            resolved_employee_name VARCHAR,
            resolved_actor_user_id VARCHAR,
            resolved_actor_name VARCHAR,
            created_at TIMESTAMPTZ NOT NULL,
            resolved_at TIMESTAMPTZ,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, create_idempotency_key),
            FOREIGN KEY (tenant_id, sku)
                REFERENCES inventory(tenant_id, sku),
            FOREIGN KEY (tenant_id, expected_location_code)
                REFERENCES locations(tenant_id, code),
            FOREIGN KEY (tenant_id, observed_location_code)
                REFERENCES locations(tenant_id, code),
            FOREIGN KEY (tenant_id, resolved_location_code)
                REFERENCES locations(tenant_id, code),
            FOREIGN KEY (tenant_id, opened_employee_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, opened_actor_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, resolved_employee_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, resolved_actor_user_id)
                REFERENCES users(tenant_id, id),
            CHECK (
                (expected_location_code IS NULL
                 AND expected_location_name IS NULL
                 AND expected_location_path IS NULL
                 AND expected_location_balance_at_open IS NULL
                 AND expected_location_version_at_open IS NULL)
                OR
                (expected_location_code IS NOT NULL
                 AND expected_location_code <> 'UNASSIGNED'
                 AND expected_location_name IS NOT NULL
                 AND expected_location_path IS NOT NULL
                 AND expected_location_balance_at_open IS NOT NULL
                 AND expected_location_version_at_open IS NOT NULL)
            ),
            CHECK (
                (observed_location_code IS NULL
                 AND observed_location_name IS NULL
                 AND observed_location_path IS NULL
                 AND observed_location_balance_at_open IS NULL
                 AND observed_location_version_at_open IS NULL)
                OR
                (observed_location_code IS NOT NULL
                 AND observed_location_code <> 'UNASSIGNED'
                 AND observed_location_name IS NOT NULL
                 AND observed_location_path IS NOT NULL
                 AND observed_location_balance_at_open IS NOT NULL
                 AND observed_location_version_at_open IS NOT NULL)
            ),
            CHECK (
                (kind = 'missing'
                 AND expected_location_code IS NOT NULL
                 AND observed_location_code IS NULL)
                OR
                (kind = 'misplaced'
                 AND expected_location_code IS NOT NULL
                 AND observed_location_code IS NOT NULL
                 AND expected_location_code <> observed_location_code)
                OR
                (kind = 'unexpected'
                 AND observed_location_code IS NOT NULL
                 AND (expected_location_code IS NULL
                      OR expected_location_code <> observed_location_code))
            ),
            CHECK (
                (resolved_location_code IS NULL
                 AND resolved_location_name IS NULL
                 AND resolved_location_path IS NULL)
                OR
                (resolved_location_code IS NOT NULL
                 AND resolved_location_code <> 'UNASSIGNED'
                 AND resolved_location_name IS NOT NULL
                 AND resolved_location_path IS NOT NULL)
            ),
            CHECK (
                (resolution = 'located'
                 AND resolved_location_code IS NOT NULL)
                OR
                ((resolution IS NULL OR resolution <> 'located')
                 AND resolved_location_code IS NULL)
            ),
            CHECK (
                (status = 'open'
                 AND resolve_idempotency_key IS NULL
                 AND resolve_fingerprint IS NULL
                 AND resolution IS NULL
                 AND resolution_reference IS NULL
                 AND resolution_reason IS NULL
                 AND resolved_employee_user_id IS NULL
                 AND resolved_employee_name IS NULL
                 AND resolved_actor_user_id IS NULL
                 AND resolved_actor_name IS NULL
                 AND resolved_at IS NULL)
                OR
                (status = 'resolved'
                 AND resolve_idempotency_key IS NOT NULL
                 AND resolve_fingerprint IS NOT NULL
                 AND resolution IS NOT NULL
                 AND resolution_reason IS NOT NULL
                 AND resolved_employee_user_id IS NOT NULL
                 AND resolved_employee_name IS NOT NULL
                 AND resolved_actor_user_id IS NOT NULL
                 AND resolved_actor_name IS NOT NULL
                 AND resolved_at IS NOT NULL)
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_investigation_events (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            investigation_id VARCHAR NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            event_type VARCHAR NOT NULL CHECK (
                event_type IN ('opened', 'evidence_added', 'resolved')
            ),
            idempotency_key VARCHAR NOT NULL,
            request_fingerprint VARCHAR NOT NULL,
            location_code VARCHAR,
            location_name VARCHAR,
            location_path VARCHAR,
            reference VARCHAR,
            note TEXT,
            reason TEXT,
            resolution VARCHAR CHECK (
                resolution IS NULL OR resolution IN (
                    'located',
                    'confirmed_missing',
                    'record_error',
                    'dismissed'
                )
            ),
            employee_user_id VARCHAR NOT NULL,
            employee_name VARCHAR NOT NULL,
            actor_user_id VARCHAR NOT NULL,
            actor_name VARCHAR NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, idempotency_key),
            UNIQUE (tenant_id, investigation_id, sequence),
            FOREIGN KEY (tenant_id, investigation_id)
                REFERENCES inventory_investigations(tenant_id, id),
            FOREIGN KEY (tenant_id, location_code)
                REFERENCES locations(tenant_id, code),
            FOREIGN KEY (tenant_id, employee_user_id)
                REFERENCES users(tenant_id, id),
            FOREIGN KEY (tenant_id, actor_user_id)
                REFERENCES users(tenant_id, id),
            CHECK (
                (location_code IS NULL
                 AND location_name IS NULL
                 AND location_path IS NULL)
                OR
                (location_code IS NOT NULL
                 AND location_code <> 'UNASSIGNED'
                 AND location_name IS NOT NULL
                 AND location_path IS NOT NULL)
            ),
            CHECK (
                (event_type = 'opened' AND sequence = 1)
                OR
                (event_type <> 'opened' AND sequence > 1)
            ),
            CHECK (
                (event_type = 'resolved' AND resolution IS NOT NULL)
                OR
                (event_type <> 'resolved' AND resolution IS NULL)
            ),
            CHECK (
                (event_type = 'opened' AND reason IS NULL)
                OR
                (event_type = 'evidence_added'
                 AND note IS NOT NULL
                 AND LENGTH(TRIM(note)) > 0
                 AND reference IS NULL
                 AND reason IS NULL)
                OR
                (event_type = 'resolved'
                 AND note IS NULL
                 AND reason IS NOT NULL
                 AND LENGTH(TRIM(reason)) > 0)
            ),
            CHECK (
                event_type <> 'resolved'
                OR
                (resolution = 'located' AND location_code IS NOT NULL)
                OR
                (resolution <> 'located' AND location_code IS NULL)
            )
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_investigations_tenant_status_created
        ON inventory_investigations(tenant_id, status, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_investigations_tenant_kind_status
        ON inventory_investigations(tenant_id, kind, status, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_investigations_tenant_sku_status
        ON inventory_investigations(tenant_id, sku, status, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_investigations_tenant_expected_location
        ON inventory_investigations(
            tenant_id, expected_location_code, status, created_at DESC
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_investigations_tenant_observed_location
        ON inventory_investigations(
            tenant_id, observed_location_code, status, created_at DESC
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_investigations_tenant_resolved_location
        ON inventory_investigations(
            tenant_id, resolved_location_code, status, created_at DESC
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_investigations_tenant_resolve_key
        ON inventory_investigations(tenant_id, resolve_idempotency_key)
        WHERE resolve_idempotency_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_investigation_events_one_resolution
        ON inventory_investigation_events(tenant_id, investigation_id)
        WHERE event_type = 'resolved'
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_investigation_events_tenant_created
        ON inventory_investigation_events(tenant_id, created_at DESC)
        """,
    )

    for statement in statements:
        execute_write(db, statement)


def _init_order_comparison_tables(db: Any) -> None:
    statements = (
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_tenant_id
        ON inventory_transactions(tenant_id, id)
        """,
        """
        CREATE TABLE IF NOT EXISTS order_comparison_batches (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            idempotency_key VARCHAR NOT NULL,
            request_fingerprint VARCHAR NOT NULL,
            sales_channel VARCHAR NOT NULL,
            source_filename VARCHAR,
            imported_by_user_id VARCHAR NOT NULL,
            imported_by_name VARCHAR NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, idempotency_key),
            FOREIGN KEY (tenant_id, imported_by_user_id)
                REFERENCES users(tenant_id, id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS order_comparison_lines (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            batch_id VARCHAR NOT NULL,
            source_row_number INTEGER NOT NULL CHECK (
                source_row_number >= 2
            ),
            order_reference VARCHAR NOT NULL,
            submitted_sku VARCHAR,
            submitted_barcode VARCHAR,
            sku VARCHAR,
            barcode VARCHAR,
            product_name VARCHAR,
            ordered_quantity INTEGER NOT NULL CHECK (
                ordered_quantity > 0
                AND ordered_quantity <= 1000000
            ),
            stockpile_quantity INTEGER NOT NULL DEFAULT 0 CHECK (
                stockpile_quantity >= 0
                AND stockpile_quantity <= 1000000
            ),
            comparison_status VARCHAR NOT NULL CHECK (
                comparison_status IN (
                    'matched',
                    'missing_removal',
                    'quantity_mismatch',
                    'unknown_product'
                )
            ),
            reservation_id VARCHAR,
            resolution VARCHAR CHECK (
                resolution IS NULL OR resolution IN (
                    'link_removal',
                    'source_corrected',
                    'accept_exception'
                )
            ),
            inventory_transaction_id VARCHAR,
            resolution_reason TEXT,
            resolve_idempotency_key VARCHAR,
            resolve_fingerprint VARCHAR,
            resolved_by_user_id VARCHAR,
            resolved_by_name VARCHAR,
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, batch_id, source_row_number),
            CHECK (
                submitted_sku IS NOT NULL
                OR submitted_barcode IS NOT NULL
            ),
            CHECK (
                (
                    comparison_status = 'unknown_product'
                    AND sku IS NULL
                    AND barcode IS NULL
                    AND product_name IS NULL
                    AND stockpile_quantity = 0
                    AND reservation_id IS NULL
                )
                OR (
                    comparison_status <> 'unknown_product'
                    AND sku IS NOT NULL
                    AND barcode IS NOT NULL
                    AND product_name IS NOT NULL
                )
            ),
            CHECK (
                (
                    comparison_status = 'matched'
                    AND stockpile_quantity = ordered_quantity
                )
                OR (
                    comparison_status = 'missing_removal'
                    AND stockpile_quantity = 0
                )
                OR (
                    comparison_status = 'quantity_mismatch'
                    AND stockpile_quantity > 0
                    AND stockpile_quantity <> ordered_quantity
                )
                OR comparison_status = 'unknown_product'
            ),
            CHECK (
                comparison_status <> 'matched'
                OR resolution IS NULL
            ),
            CHECK (
                (
                    resolution IS NULL
                    AND resolution_reason IS NULL
                    AND resolve_idempotency_key IS NULL
                    AND resolve_fingerprint IS NULL
                    AND resolved_by_user_id IS NULL
                    AND resolved_by_name IS NULL
                    AND resolved_at IS NULL
                )
                OR (
                    resolution IS NOT NULL
                    AND resolution_reason IS NOT NULL
                    AND resolve_idempotency_key IS NOT NULL
                    AND resolve_fingerprint IS NOT NULL
                    AND resolved_by_user_id IS NOT NULL
                    AND resolved_by_name IS NOT NULL
                    AND resolved_at IS NOT NULL
                )
            ),
            CHECK (
                (
                    resolution = 'link_removal'
                    AND inventory_transaction_id IS NOT NULL
                )
                OR (
                    resolution IN (
                        'source_corrected',
                        'accept_exception'
                    )
                    AND inventory_transaction_id IS NULL
                )
                OR (
                    resolution IS NULL
                    AND inventory_transaction_id IS NULL
                )
            ),
            FOREIGN KEY (tenant_id, batch_id)
                REFERENCES order_comparison_batches(tenant_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, sku)
                REFERENCES inventory(tenant_id, sku),
            FOREIGN KEY (tenant_id, reservation_id)
                REFERENCES inventory_reservations(tenant_id, id),
            FOREIGN KEY (tenant_id, inventory_transaction_id)
                REFERENCES inventory_transactions(tenant_id, id),
            FOREIGN KEY (tenant_id, resolved_by_user_id)
                REFERENCES users(tenant_id, id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_order_comparison_batches_tenant_created
        ON order_comparison_batches(tenant_id, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_order_comparison_lines_tenant_batch_status
        ON order_comparison_lines(
            tenant_id, batch_id, comparison_status, source_row_number
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_order_comparison_lines_tenant_reference
        ON order_comparison_lines(
            tenant_id, order_reference, sku, created_at DESC
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_order_comparison_lines_tenant_open
        ON order_comparison_lines(
            tenant_id, comparison_status, created_at DESC
        )
        WHERE resolution IS NULL
          AND comparison_status <> 'matched'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_order_comparison_lines_tenant_resolve_key
        ON order_comparison_lines(tenant_id, resolve_idempotency_key)
        WHERE resolve_idempotency_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_order_comparison_lines_tenant_transaction
        ON order_comparison_lines(tenant_id, inventory_transaction_id)
        WHERE inventory_transaction_id IS NOT NULL
        """,
    )

    for statement in statements:
        execute_write(db, statement)


def _init_ai_review_tables(db: Any) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS ai_reviews (
            id VARCHAR PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            idempotency_key VARCHAR NOT NULL,
            request_fingerprint VARCHAR NOT NULL,
            review_type VARCHAR NOT NULL CHECK (
                review_type IN (
                    'delivery_document',
                    'discrepancy_review'
                )
            ),
            status VARCHAR NOT NULL CHECK (
                status IN (
                    'processing',
                    'completed',
                    'failed'
                )
            ),
            provider VARCHAR NOT NULL,
            model VARCHAR NOT NULL,
            prompt_version VARCHAR NOT NULL,
            source_filename VARCHAR,
            source_media_type VARCHAR,
            source_size_bytes INTEGER CHECK (
                source_size_bytes IS NULL
                OR source_size_bytes > 0
            ),
            source_sha256 VARCHAR,
            window_hours INTEGER CHECK (
                window_hours IS NULL
                OR (
                    window_hours >= 1
                    AND window_hours <= 168
                )
            ),
            evidence_json TEXT NOT NULL,
            result_json TEXT,
            error_code VARCHAR,
            error_message TEXT,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (
                attempts >= 0
            ),
            prompt_tokens INTEGER CHECK (
                prompt_tokens IS NULL
                OR prompt_tokens >= 0
            ),
            completion_tokens INTEGER CHECK (
                completion_tokens IS NULL
                OR completion_tokens >= 0
            ),
            total_tokens INTEGER CHECK (
                total_tokens IS NULL
                OR total_tokens >= 0
            ),
            latency_ms INTEGER CHECK (
                latency_ms IS NULL
                OR latency_ms >= 0
            ),
            provider_request_id VARCHAR,
            requested_by_user_id VARCHAR NOT NULL,
            requested_by_name VARCHAR NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            last_attempt_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, idempotency_key),
            CHECK (
                (
                    review_type = 'delivery_document'
                    AND source_filename IS NOT NULL
                    AND source_media_type IS NOT NULL
                    AND source_size_bytes IS NOT NULL
                    AND source_sha256 IS NOT NULL
                    AND window_hours IS NULL
                )
                OR (
                    review_type = 'discrepancy_review'
                    AND source_filename IS NULL
                    AND source_media_type IS NULL
                    AND source_size_bytes IS NULL
                    AND source_sha256 IS NULL
                    AND window_hours IS NOT NULL
                )
            ),
            CHECK (
                (
                    status = 'processing'
                    AND result_json IS NULL
                    AND error_code IS NULL
                    AND error_message IS NULL
                    AND completed_at IS NULL
                )
                OR (
                    status = 'completed'
                    AND result_json IS NOT NULL
                    AND error_code IS NULL
                    AND error_message IS NULL
                    AND completed_at IS NOT NULL
                )
                OR (
                    status = 'failed'
                    AND result_json IS NULL
                    AND error_code IS NOT NULL
                    AND error_message IS NOT NULL
                    AND completed_at IS NOT NULL
                )
            ),
            FOREIGN KEY (tenant_id, requested_by_user_id)
                REFERENCES users(tenant_id, id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_ai_reviews_tenant_created
        ON ai_reviews(tenant_id, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_ai_reviews_tenant_type_created
        ON ai_reviews(tenant_id, review_type, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_ai_reviews_tenant_status_attempt
        ON ai_reviews(tenant_id, status, last_attempt_at)
        """,
    )

    for statement in statements:
        execute_write(db, statement)


def _init_investigation_event_guards(db: Any) -> None:
    if is_sqlite(db):
        statements = (
            """
            CREATE TRIGGER IF NOT EXISTS trg_investigation_events_no_update
            BEFORE UPDATE ON inventory_investigation_events
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'Inventory investigation events are immutable.'
                );
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_investigation_events_no_delete
            BEFORE DELETE ON inventory_investigation_events
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'Inventory investigation events are immutable.'
                );
            END
            """,
        )
    else:
        statements = (
            """
            CREATE OR REPLACE FUNCTION stockpile_reject_investigation_event_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION
                    'Inventory investigation events are immutable.'
                    USING ERRCODE = '23514';
            END;
            $$ LANGUAGE plpgsql
            """,
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = 'trg_investigation_events_immutable'
                      AND tgrelid =
                          'inventory_investigation_events'::regclass
                      AND NOT tgisinternal
                ) THEN
                    EXECUTE
                        'CREATE TRIGGER trg_investigation_events_immutable '
                        'BEFORE UPDATE OR DELETE '
                        'ON inventory_investigation_events '
                        'FOR EACH ROW EXECUTE FUNCTION '
                        'stockpile_reject_investigation_event_mutation()';
                END IF;
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END;
            $$
            """,
        )

    for statement in statements:
        execute_write(db, statement)


def _init_sqlite() -> None:
    path = database_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory (
                    tenant_id TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    barcode TEXT,
                    product_name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'General',
                    unit TEXT NOT NULL DEFAULT 'unit',
                    variant TEXT,
                    acquisition_cost_centavos INTEGER
                        CHECK (
                            acquisition_cost_centavos IS NULL
                            OR acquisition_cost_centavos BETWEEN 0 AND 1000000000
                        ),
                    image_url TEXT,
                    image_storage_key TEXT,
                    track_individually INTEGER NOT NULL DEFAULT 0
                        CHECK (track_individually IN (0, 1)),
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    current_stock INTEGER NOT NULL DEFAULT 0 CHECK (current_stock >= 0),
                    status TEXT NOT NULL DEFAULT 'in_stock',
                    last_updated TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, sku)
                )
                """
            )

            inventory_columns = _sqlite_columns(connection, "inventory")
            if "barcode" not in inventory_columns:
                connection.execute("ALTER TABLE inventory ADD COLUMN barcode TEXT")
            if "version" not in inventory_columns:
                connection.execute(
                    "ALTER TABLE inventory ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
                )
            if "unit" not in inventory_columns:
                connection.execute(
                    "ALTER TABLE inventory ADD COLUMN unit TEXT NOT NULL DEFAULT 'unit'"
                )
            if "variant" not in inventory_columns:
                connection.execute("ALTER TABLE inventory ADD COLUMN variant TEXT")
            if "acquisition_cost_centavos" not in inventory_columns:
                connection.execute(
                    "ALTER TABLE inventory ADD COLUMN acquisition_cost_centavos "
                    "INTEGER CHECK (acquisition_cost_centavos IS NULL OR "
                    "acquisition_cost_centavos BETWEEN 0 AND 1000000000)"
                )
            if "image_url" not in inventory_columns:
                connection.execute("ALTER TABLE inventory ADD COLUMN image_url TEXT")
            if "image_storage_key" not in inventory_columns:
                connection.execute(
                    "ALTER TABLE inventory ADD COLUMN image_storage_key TEXT"
                )
            if "track_individually" not in inventory_columns:
                connection.execute(
                    "ALTER TABLE inventory ADD COLUMN track_individually INTEGER "
                    "NOT NULL DEFAULT 0 CHECK (track_individually IN (0, 1))"
                )
            if "active" not in inventory_columns:
                connection.execute(
                    "ALTER TABLE inventory ADD COLUMN active INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (active IN (0, 1))"
                )

            connection.execute(
                """
                UPDATE inventory
                SET barcode = 'SKU-' || sku
                WHERE barcode IS NULL OR TRIM(barcode) = ''
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_tenant_barcode "
                "ON inventory(tenant_id, barcode)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_inventory_tenant ON inventory(tenant_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_inventory_tenant_active "
                "ON inventory(tenant_id, active, product_name)"
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    tenant_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (
                        kind IN ('warehouse', 'zone', 'rack', 'bin', 'receiving',
                                 'staging', 'dispatch', 'holding')
                    ),
                    parent_code TEXT,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    stockable INTEGER NOT NULL DEFAULT 1 CHECK (stockable IN (0, 1)),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, code),
                    FOREIGN KEY (tenant_id, parent_code)
                        REFERENCES locations(tenant_id, code)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_locations_tenant_parent "
                "ON locations(tenant_id, parent_code, code)"
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory_positions (
                    tenant_id TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    location_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                    last_updated TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, sku, location_code),
                    FOREIGN KEY (tenant_id, sku)
                        REFERENCES inventory(tenant_id, sku),
                    FOREIGN KEY (tenant_id, location_code)
                        REFERENCES locations(tenant_id, code)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_positions_tenant_location "
                "ON inventory_positions(tenant_id, location_code, sku)"
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('operator', 'admin')),
                    password_hash TEXT NOT NULL,
                    badge_code_digest TEXT,
                    badge_pin_hash TEXT,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    UNIQUE (tenant_id, username)
                )
                """
            )
            user_columns = _sqlite_columns(connection, "users")
            if "badge_code_digest" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN badge_code_digest TEXT"
                )
            if "badge_pin_hash" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN badge_pin_hash TEXT"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_tenant_active "
                "ON users(tenant_id, active, display_name)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tenant_badge "
                "ON users(tenant_id, badge_code_digest) "
                "WHERE badge_code_digest IS NOT NULL"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tenant_id "
                "ON users(tenant_id, id)"
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory_transactions (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    barcode TEXT NOT NULL,
                    movement_type TEXT NOT NULL
                        CHECK (movement_type IN ('stock_in', 'stock_out', 'reversal')),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    quantity_delta INTEGER NOT NULL,
                    balance_before INTEGER NOT NULL CHECK (balance_before >= 0),
                    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
                    actor_user_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    location_code TEXT NOT NULL DEFAULT 'UNASSIGNED',
                    location_name TEXT NOT NULL DEFAULT 'Location not recorded',
                    location_path TEXT NOT NULL DEFAULT 'Location not recorded',
                    location_balance_before INTEGER NOT NULL DEFAULT 0
                        CHECK (location_balance_before >= 0),
                    location_balance_after INTEGER NOT NULL DEFAULT 0
                        CHECK (location_balance_after >= 0),
                    reason TEXT,
                    source TEXT NOT NULL DEFAULT 'scanner',
                    created_at TEXT NOT NULL,
                    reverses_transaction_id TEXT,
                    reversed_by_transaction_id TEXT,
                    UNIQUE (tenant_id, idempotency_key),
                    FOREIGN KEY (actor_user_id) REFERENCES users(id),
                    FOREIGN KEY (tenant_id, location_code)
                        REFERENCES locations(tenant_id, code),
                    FOREIGN KEY (reverses_transaction_id) REFERENCES inventory_transactions(id),
                    FOREIGN KEY (reversed_by_transaction_id) REFERENCES inventory_transactions(id)
                )
                """
            )
            transaction_columns = _sqlite_columns(
                connection, "inventory_transactions"
            )
            if "location_code" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN "
                    "location_code TEXT NOT NULL DEFAULT 'UNASSIGNED'"
                )
            if "location_name" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN "
                    "location_name TEXT NOT NULL DEFAULT 'Location not recorded'"
                )
            if "location_path" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN "
                    "location_path TEXT NOT NULL DEFAULT 'Location not recorded'"
                )
            if "location_balance_before" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN "
                    "location_balance_before INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE inventory_transactions "
                    "SET location_balance_before = balance_before"
                )
            if "location_balance_after" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN "
                    "location_balance_after INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE inventory_transactions "
                    "SET location_balance_after = balance_after"
                )
            connection.execute(
                """
                UPDATE inventory_transactions
                SET location_name = 'Location not recorded',
                    location_path = 'Location not recorded'
                WHERE location_code = 'UNASSIGNED'
                  AND (location_name = 'Needs placement'
                       OR location_path = 'Needs placement')
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_tenant_created "
                "ON inventory_transactions(tenant_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_tenant_location "
                "ON inventory_transactions(tenant_id, location_code, created_at DESC)"
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory_transfers (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    create_idempotency_key TEXT NOT NULL,
                    receive_idempotency_key TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending',
                            'received',
                            'received_with_discrepancy',
                            'cancelled'
                        )),
                    source_location_code TEXT NOT NULL,
                    source_location_name TEXT NOT NULL,
                    source_location_path TEXT NOT NULL,
                    destination_location_code TEXT NOT NULL,
                    destination_location_name TEXT NOT NULL,
                    destination_location_path TEXT NOT NULL,
                    initiated_by_user_id TEXT NOT NULL,
                    initiated_by_name TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    received_by_user_id TEXT,
                    received_by_name TEXT,
                    receive_note TEXT,
                    received_at TEXT,
                    UNIQUE (tenant_id, create_idempotency_key),
                    UNIQUE (tenant_id, id),
                    CHECK (source_location_code <> destination_location_code),
                    FOREIGN KEY (tenant_id, source_location_code)
                        REFERENCES locations(tenant_id, code),
                    FOREIGN KEY (tenant_id, destination_location_code)
                        REFERENCES locations(tenant_id, code),
                    FOREIGN KEY (tenant_id, initiated_by_user_id)
                        REFERENCES users(tenant_id, id),
                    FOREIGN KEY (tenant_id, received_by_user_id)
                        REFERENCES users(tenant_id, id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory_transfer_lines (
                    transfer_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    barcode TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    quantity_sent INTEGER NOT NULL
                        CHECK (quantity_sent > 0),
                    quantity_received INTEGER
                        CHECK (quantity_received >= 0),
                    discrepancy_note TEXT,
                    source_balance_before INTEGER
                        CHECK (source_balance_before >= 0),
                    source_balance_after INTEGER
                        CHECK (source_balance_after >= 0),
                    destination_balance_before INTEGER
                        CHECK (destination_balance_before >= 0),
                    destination_balance_after INTEGER
                        CHECK (destination_balance_after >= 0),
                    PRIMARY KEY (tenant_id, transfer_id, sku),
                    FOREIGN KEY (tenant_id, transfer_id)
                        REFERENCES inventory_transfers(tenant_id, id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (tenant_id, sku)
                        REFERENCES inventory(tenant_id, sku)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_transfers_tenant_status_created "
                "ON inventory_transfers(tenant_id, status, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_transfers_tenant_destination "
                "ON inventory_transfers("
                "tenant_id, destination_location_code, status, created_at DESC)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_tenant_receive_key "
                "ON inventory_transfers(tenant_id, receive_idempotency_key) "
                "WHERE receive_idempotency_key IS NOT NULL"
            )

            _init_stock_control_tables(connection)
            _init_investigation_tables(connection)
            _init_investigation_event_guards(connection)
            _init_order_comparison_tables(connection)
            _init_ai_review_tables(connection)

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sheets_outbox (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'synced', 'failed', 'not_configured')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_attempt_at TEXT,
                    synced_at TEXT,
                    claim_token TEXT,
                    claimed_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (transaction_id) REFERENCES inventory_transactions(id)
                )
                """
            )
            outbox_columns = _sqlite_columns(connection, "sheets_outbox")
            if "claim_token" not in outbox_columns:
                connection.execute("ALTER TABLE sheets_outbox ADD COLUMN claim_token TEXT")
            if "claimed_at" not in outbox_columns:
                connection.execute("ALTER TABLE sheets_outbox ADD COLUMN claimed_at TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_tenant_status "
                "ON sheets_outbox(tenant_id, status, created_at)"
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory_imports (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created INTEGER NOT NULL DEFAULT 0 CHECK (created >= 0),
                    updated INTEGER NOT NULL DEFAULT 0 CHECK (updated >= 0),
                    skipped INTEGER NOT NULL DEFAULT 0 CHECK (skipped >= 0),
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    UNIQUE (tenant_id, idempotency_key)
                )
                """
            )

            _seed_sqlite_users(connection)
            _seed_sqlite_unassigned_location(connection)
            if seed_demo_data():
                _migrate_sqlite_demo_inventory(connection)
                inserted_demo_skus = _seed_sqlite_inventory(connection)
                _seed_sqlite_locations(connection)
                _seed_sqlite_demo_positions(connection, inserted_demo_skus)
            _migrate_sqlite_inventory_positions(connection)
    finally:
        connection.close()


def _seed_sqlite_users(connection: sqlite3.Connection) -> None:
    tenant = tenant_id()
    timestamp = utc_now()
    include_demo_badges = seed_demo_data()
    configured_users = [
        (
            "admin",
            os.getenv("STOCKPILE_ADMIN_USERNAME", "admin").strip().lower(),
            os.getenv("STOCKPILE_ADMIN_DISPLAY_NAME", "Alex Morgan").strip(),
            os.getenv("STOCKPILE_ADMIN_PASSWORD", ""),
            os.getenv(
                "STOCKPILE_ADMIN_BADGE_CODE",
                "ADM-1001" if include_demo_badges else "",
            ).strip(),
            os.getenv("STOCKPILE_ADMIN_BADGE_PIN", "").strip(),
        ),
        (
            "operator",
            os.getenv("STOCKPILE_OPERATOR_USERNAME", "operator").strip().lower(),
            os.getenv("STOCKPILE_OPERATOR_DISPLAY_NAME", "Jordan Lee").strip(),
            os.getenv("STOCKPILE_OPERATOR_PASSWORD", ""),
            os.getenv(
                "STOCKPILE_OPERATOR_BADGE_CODE",
                "OPS-2042" if include_demo_badges else "",
            ).strip(),
            os.getenv("STOCKPILE_OPERATOR_BADGE_PIN", "").strip(),
        ),
    ]
    for (
        role,
        username,
        display_name,
        password,
        badge_code,
        badge_pin,
    ) in configured_users:
        if not username or not password:
            continue
        password_hash = hash_password(password)
        badge_digest = badge_code_digest(badge_code, tenant) if badge_code else None
        badge_pin_hash = hash_badge_pin(badge_pin) if badge_pin else None
        connection.execute(
            """
            INSERT INTO users
                (id, tenant_id, username, display_name, role, password_hash,
                 badge_code_digest, badge_pin_hash, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                username = excluded.username,
                display_name = excluded.display_name,
                role = excluded.role,
                password_hash = excluded.password_hash,
                badge_code_digest = COALESCE(
                    excluded.badge_code_digest,
                    users.badge_code_digest
                ),
                badge_pin_hash = CASE
                    WHEN excluded.badge_code_digest IS NOT NULL
                    THEN excluded.badge_pin_hash
                    ELSE users.badge_pin_hash
                END,
                active = 1
            """,
            (
                f"{tenant}-{role}",
                tenant,
                username,
                display_name or role.title(),
                role,
                password_hash,
                badge_digest,
                badge_pin_hash,
                timestamp,
            ),
        )


def _migrate_sqlite_demo_inventory(connection: sqlite3.Connection) -> None:
    tenant = tenant_id()
    for sku, old_barcode, old_name, old_category in LEGACY_DEMO_INVENTORY:
        _, new_barcode, new_name, new_category, _ = DEMO_INVENTORY_BY_SKU[sku]
        has_history = connection.execute(
            "SELECT 1 FROM inventory_transactions WHERE tenant_id = ? AND sku = ? LIMIT 1",
            (tenant, sku),
        ).fetchone()
        selected_barcode = old_barcode if has_history else new_barcode
        if selected_barcode != old_barcode:
            collision = connection.execute(
                "SELECT 1 FROM inventory WHERE tenant_id = ? AND barcode = ? AND sku <> ? LIMIT 1",
                (tenant, selected_barcode, sku),
            ).fetchone()
            if collision:
                selected_barcode = old_barcode

        connection.execute(
            """
            UPDATE inventory
            SET barcode = ?, product_name = ?, category = ?
            WHERE tenant_id = ? AND sku = ? AND barcode = ?
              AND product_name = ? AND category = ?
            """,
            (
                selected_barcode,
                new_name,
                new_category,
                tenant,
                sku,
                old_barcode,
                old_name,
                old_category,
            ),
        )

def _seed_sqlite_inventory(connection: sqlite3.Connection) -> set[str]:
    timestamp = utc_now()
    tenant = tenant_id()
    existing_demo_skus = {
        row[0]
        for row in connection.execute(
            "SELECT sku FROM inventory WHERE tenant_id = ?", (tenant,)
        )
        if row[0] in DEMO_INVENTORY_BY_SKU
    }
    connection.executemany(
        """
        INSERT INTO inventory
            (tenant_id, sku, barcode, product_name, category, current_stock, status, last_updated, version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT DO NOTHING
        """,
        [
            (
                tenant,
                sku,
                barcode,
                name,
                category,
                stock,
                "in_stock" if stock > 0 else "out_of_stock",
                timestamp,
            )
            for sku, barcode, name, category, stock in DEMO_INVENTORY
        ],
    )
    return set(DEMO_INVENTORY_BY_SKU) - existing_demo_skus


def _seed_sqlite_unassigned_location(connection: sqlite3.Connection) -> None:
    timestamp = utc_now()
    tenants = {tenant_id()}
    for table in ("inventory", "users", "inventory_transactions"):
        tenants.update(
            row[0]
            for row in connection.execute(
                f"SELECT DISTINCT tenant_id FROM {table}"
            )
        )
    code, name, kind, parent_code, stockable = UNASSIGNED_LOCATION
    connection.executemany(
        """
        INSERT INTO locations
            (tenant_id, code, name, kind, parent_code, active, stockable, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(tenant_id, code) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            parent_code = excluded.parent_code,
            active = excluded.active,
            stockable = excluded.stockable
        """,
        [
            (tenant, code, name, kind, parent_code, int(stockable), timestamp)
            for tenant in tenants
        ],
    )


def _seed_sqlite_locations(connection: sqlite3.Connection) -> None:
    tenant = tenant_id()
    timestamp = utc_now()
    connection.executemany(
        """
        INSERT INTO locations
            (tenant_id, code, name, kind, parent_code, active, stockable, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(tenant_id, code) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            parent_code = excluded.parent_code,
            active = excluded.active,
            stockable = excluded.stockable
        """,
        [
            (tenant, code, name, kind, parent_code, int(stockable), timestamp)
            for code, name, kind, parent_code, stockable in DEMO_LOCATIONS
        ],
    )


def _demo_position_rows(
    inserted_skus: set[str], timestamp: str
) -> list[tuple[str, str, int, str]]:
    rows: list[tuple[str, str, int, str]] = []
    for sku, _barcode, _name, category, stock in DEMO_INVENTORY:
        if sku not in inserted_skus or stock <= 0:
            continue
        primary_code, secondary_code = DEMO_CATEGORY_LOCATIONS[category]
        primary_quantity = stock if stock < 10 else (stock * 3) // 4
        secondary_quantity = stock - primary_quantity
        rows.append((sku, primary_code, primary_quantity, timestamp))
        if secondary_quantity:
            rows.append((sku, secondary_code, secondary_quantity, timestamp))
    return rows


def _seed_sqlite_demo_positions(
    connection: sqlite3.Connection, inserted_skus: set[str]
) -> None:
    tenant = tenant_id()
    connection.executemany(
        """
        INSERT INTO inventory_positions
            (tenant_id, sku, location_code, quantity, last_updated, version)
        VALUES (?, ?, ?, ?, ?, 0)
        ON CONFLICT(tenant_id, sku, location_code) DO NOTHING
        """,
        [
            (tenant, sku, location_code, quantity, timestamp)
            for sku, location_code, quantity, timestamp
            in _demo_position_rows(inserted_skus, utc_now())
        ],
    )


def _migrate_sqlite_inventory_positions(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """
        INSERT INTO inventory_positions
            (tenant_id, sku, location_code, quantity, last_updated, version)
        SELECT i.tenant_id, i.sku, 'UNASSIGNED', i.current_stock,
               i.last_updated, 0
        FROM inventory AS i
        WHERE NOT EXISTS (
            SELECT 1
            FROM inventory_positions AS p
            WHERE p.tenant_id = i.tenant_id AND p.sku = i.sku
        )
        """
    )


def _init_postgres() -> None:
    connection = psycopg2.connect(database_url(), cursor_factory=RealDictCursor)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inventory (
                        tenant_id VARCHAR NOT NULL,
                        sku VARCHAR NOT NULL,
                        barcode VARCHAR,
                        product_name VARCHAR NOT NULL,
                        category VARCHAR NOT NULL DEFAULT 'General',
                        unit VARCHAR NOT NULL DEFAULT 'unit',
                        variant VARCHAR,
                        acquisition_cost_centavos INTEGER
                            CHECK (
                                acquisition_cost_centavos IS NULL
                                OR acquisition_cost_centavos BETWEEN 0 AND 1000000000
                            ),
                        image_url VARCHAR(2048),
                        image_storage_key VARCHAR(512),
                        track_individually BOOLEAN NOT NULL DEFAULT FALSE,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        current_stock INTEGER NOT NULL DEFAULT 0 CHECK (current_stock >= 0),
                        status VARCHAR NOT NULL DEFAULT 'in_stock',
                        last_updated TIMESTAMPTZ NOT NULL,
                        version INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (tenant_id, sku)
                    )
                    """
                )
                cursor.execute("ALTER TABLE inventory ADD COLUMN IF NOT EXISTS barcode VARCHAR")
                cursor.execute(
                    "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 0"
                )
                cursor.execute(
                    "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS "
                    "unit VARCHAR NOT NULL DEFAULT 'unit'"
                )
                cursor.execute(
                    "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS variant VARCHAR"
                )
                cursor.execute(
                    "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS "
                    "acquisition_cost_centavos INTEGER CHECK "
                    "(acquisition_cost_centavos IS NULL OR "
                    "acquisition_cost_centavos BETWEEN 0 AND 1000000000)"
                )
                cursor.execute(
                    "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS "
                    "image_url VARCHAR(2048)"
                )
                cursor.execute(
                    "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS "
                    "image_storage_key VARCHAR(512)"
                )
                cursor.execute(
                    "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS "
                    "track_individually BOOLEAN NOT NULL DEFAULT FALSE"
                )
                cursor.execute(
                    "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS "
                    "active BOOLEAN NOT NULL DEFAULT TRUE"
                )
                cursor.execute(
                    "UPDATE inventory SET barcode = 'SKU-' || sku "
                    "WHERE barcode IS NULL OR BTRIM(barcode) = ''"
                )
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_tenant_barcode "
                    "ON inventory(tenant_id, barcode)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_inventory_tenant_active "
                    "ON inventory(tenant_id, active, product_name)"
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS locations (
                        tenant_id VARCHAR NOT NULL,
                        code VARCHAR NOT NULL,
                        name VARCHAR NOT NULL,
                        kind VARCHAR NOT NULL CHECK (
                            kind IN ('warehouse', 'zone', 'rack', 'bin', 'receiving',
                                     'staging', 'dispatch', 'holding')
                        ),
                        parent_code VARCHAR,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        stockable BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL,
                        PRIMARY KEY (tenant_id, code),
                        FOREIGN KEY (tenant_id, parent_code)
                            REFERENCES locations(tenant_id, code)
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_locations_tenant_parent "
                    "ON locations(tenant_id, parent_code, code)"
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inventory_positions (
                        tenant_id VARCHAR NOT NULL,
                        sku VARCHAR NOT NULL,
                        location_code VARCHAR NOT NULL,
                        quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                        last_updated TIMESTAMPTZ NOT NULL,
                        version INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (tenant_id, sku, location_code),
                        FOREIGN KEY (tenant_id, sku)
                            REFERENCES inventory(tenant_id, sku),
                        FOREIGN KEY (tenant_id, location_code)
                            REFERENCES locations(tenant_id, code)
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_positions_tenant_location "
                    "ON inventory_positions(tenant_id, location_code, sku)"
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id VARCHAR PRIMARY KEY,
                        tenant_id VARCHAR NOT NULL,
                        username VARCHAR NOT NULL,
                        display_name VARCHAR NOT NULL,
                        role VARCHAR NOT NULL CHECK (role IN ('operator', 'admin')),
                        password_hash VARCHAR NOT NULL,
                        badge_code_digest VARCHAR,
                        badge_pin_hash VARCHAR,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL,
                        UNIQUE (tenant_id, username)
                    )
                    """
                )
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                    "badge_code_digest VARCHAR"
                )
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                    "badge_pin_hash VARCHAR"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_users_tenant_active "
                    "ON users(tenant_id, active, display_name)"
                )
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tenant_badge "
                    "ON users(tenant_id, badge_code_digest) "
                    "WHERE badge_code_digest IS NOT NULL"
                )
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tenant_id "
                    "ON users(tenant_id, id)"
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inventory_transactions (
                        id VARCHAR PRIMARY KEY,
                        tenant_id VARCHAR NOT NULL,
                        idempotency_key VARCHAR NOT NULL,
                        sku VARCHAR NOT NULL,
                        barcode VARCHAR NOT NULL,
                        movement_type VARCHAR NOT NULL
                            CHECK (movement_type IN ('stock_in', 'stock_out', 'reversal')),
                        quantity INTEGER NOT NULL CHECK (quantity > 0),
                        quantity_delta INTEGER NOT NULL,
                        balance_before INTEGER NOT NULL CHECK (balance_before >= 0),
                        balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
                        actor_user_id VARCHAR NOT NULL REFERENCES users(id),
                        actor_name VARCHAR NOT NULL,
                        location_code VARCHAR NOT NULL DEFAULT 'UNASSIGNED',
                        location_name VARCHAR NOT NULL DEFAULT 'Location not recorded',
                        location_path VARCHAR NOT NULL DEFAULT 'Location not recorded',
                        location_balance_before INTEGER NOT NULL DEFAULT 0
                            CHECK (location_balance_before >= 0),
                        location_balance_after INTEGER NOT NULL DEFAULT 0
                            CHECK (location_balance_after >= 0),
                        reason TEXT,
                        source VARCHAR NOT NULL DEFAULT 'scanner',
                        created_at TIMESTAMPTZ NOT NULL,
                        reverses_transaction_id VARCHAR REFERENCES inventory_transactions(id),
                        reversed_by_transaction_id VARCHAR REFERENCES inventory_transactions(id),
                        FOREIGN KEY (tenant_id, location_code)
                            REFERENCES locations(tenant_id, code),
                        UNIQUE (tenant_id, idempotency_key)
                    )
                    """
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS "
                    "location_code VARCHAR"
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS "
                    "location_name VARCHAR"
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS "
                    "location_path VARCHAR"
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS "
                    "location_balance_before INTEGER"
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS "
                    "location_balance_after INTEGER"
                )
                cursor.execute(
                    """
                    UPDATE inventory_transactions
                    SET location_code = COALESCE(location_code, 'UNASSIGNED'),
                        location_name = COALESCE(location_name, 'Location not recorded'),
                        location_path = COALESCE(location_path, 'Location not recorded'),
                        location_balance_before = COALESCE(location_balance_before, balance_before),
                        location_balance_after = COALESCE(location_balance_after, balance_after)
                    WHERE location_code IS NULL OR location_name IS NULL
                       OR location_path IS NULL OR location_balance_before IS NULL
                       OR location_balance_after IS NULL
                    """
                )
                cursor.execute(
                    """
                    UPDATE inventory_transactions
                    SET location_name = 'Location not recorded',
                        location_path = 'Location not recorded'
                    WHERE location_code = 'UNASSIGNED'
                      AND (location_name = 'Needs placement'
                           OR location_path = 'Needs placement')
                    """
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ALTER COLUMN location_code "
                    "SET DEFAULT 'UNASSIGNED'"
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ALTER COLUMN location_name "
                    "SET DEFAULT 'Location not recorded'"
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ALTER COLUMN location_path "
                    "SET DEFAULT 'Location not recorded'"
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ALTER COLUMN location_balance_before "
                    "SET DEFAULT 0"
                )
                cursor.execute(
                    "ALTER TABLE inventory_transactions ALTER COLUMN location_balance_after "
                    "SET DEFAULT 0"
                )
                for column in (
                    "location_code",
                    "location_name",
                    "location_path",
                    "location_balance_before",
                    "location_balance_after",
                ):
                    cursor.execute(
                        f"ALTER TABLE inventory_transactions ALTER COLUMN {column} SET NOT NULL"
                    )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_transactions_tenant_created "
                    "ON inventory_transactions(tenant_id, created_at DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_transactions_tenant_location "
                    "ON inventory_transactions(tenant_id, location_code, created_at DESC)"
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inventory_transfers (
                        id VARCHAR PRIMARY KEY,
                        tenant_id VARCHAR NOT NULL,
                        create_idempotency_key VARCHAR NOT NULL,
                        receive_idempotency_key VARCHAR,
                        status VARCHAR NOT NULL DEFAULT 'pending'
                            CHECK (status IN (
                                'pending',
                                'received',
                                'received_with_discrepancy',
                                'cancelled'
                            )),
                        source_location_code VARCHAR NOT NULL,
                        source_location_name VARCHAR NOT NULL,
                        source_location_path VARCHAR NOT NULL,
                        destination_location_code VARCHAR NOT NULL,
                        destination_location_name VARCHAR NOT NULL,
                        destination_location_path VARCHAR NOT NULL,
                        initiated_by_user_id VARCHAR NOT NULL,
                        initiated_by_name VARCHAR NOT NULL,
                        note TEXT,
                        created_at TIMESTAMPTZ NOT NULL,
                        received_by_user_id VARCHAR,
                        received_by_name VARCHAR,
                        receive_note TEXT,
                        received_at TIMESTAMPTZ,
                        FOREIGN KEY (tenant_id, source_location_code)
                            REFERENCES locations(tenant_id, code),
                        FOREIGN KEY (tenant_id, destination_location_code)
                            REFERENCES locations(tenant_id, code),
                        FOREIGN KEY (tenant_id, initiated_by_user_id)
                            REFERENCES users(tenant_id, id),
                        FOREIGN KEY (tenant_id, received_by_user_id)
                            REFERENCES users(tenant_id, id),
                        UNIQUE (tenant_id, id),
                        UNIQUE (tenant_id, create_idempotency_key),
                        CHECK (source_location_code <> destination_location_code)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inventory_transfer_lines (
                        transfer_id VARCHAR NOT NULL,
                        tenant_id VARCHAR NOT NULL,
                        sku VARCHAR NOT NULL,
                        barcode VARCHAR NOT NULL,
                        product_name VARCHAR NOT NULL,
                        quantity_sent INTEGER NOT NULL
                            CHECK (quantity_sent > 0),
                        quantity_received INTEGER
                            CHECK (quantity_received >= 0),
                        discrepancy_note TEXT,
                        source_balance_before INTEGER
                            CHECK (source_balance_before >= 0),
                        source_balance_after INTEGER
                            CHECK (source_balance_after >= 0),
                        destination_balance_before INTEGER
                            CHECK (destination_balance_before >= 0),
                        destination_balance_after INTEGER
                            CHECK (destination_balance_after >= 0),
                        PRIMARY KEY (tenant_id, transfer_id, sku),
                        FOREIGN KEY (tenant_id, transfer_id)
                            REFERENCES inventory_transfers(tenant_id, id)
                            ON DELETE CASCADE,
                        FOREIGN KEY (tenant_id, sku)
                            REFERENCES inventory(tenant_id, sku)
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_transfers_tenant_status_created "
                    "ON inventory_transfers(tenant_id, status, created_at DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_transfers_tenant_destination "
                    "ON inventory_transfers("
                    "tenant_id, destination_location_code, status, created_at DESC)"
                )
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_tenant_receive_key "
                    "ON inventory_transfers(tenant_id, receive_idempotency_key) "
                    "WHERE receive_idempotency_key IS NOT NULL"
                )
                _init_stock_control_tables(connection)
                _init_investigation_tables(connection)
                _init_investigation_event_guards(connection)
                _init_order_comparison_tables(connection)
                _init_ai_review_tables(connection)
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sheets_outbox (
                        id VARCHAR PRIMARY KEY,
                        tenant_id VARCHAR NOT NULL,
                        transaction_id VARCHAR NOT NULL UNIQUE REFERENCES inventory_transactions(id),
                        status VARCHAR NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'synced', 'failed', 'not_configured')),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        last_attempt_at TIMESTAMPTZ,
                        synced_at TIMESTAMPTZ,
                        claim_token VARCHAR,
                        claimed_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cursor.execute(
                    "ALTER TABLE sheets_outbox ADD COLUMN IF NOT EXISTS claim_token VARCHAR"
                )
                cursor.execute(
                    "ALTER TABLE sheets_outbox ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_outbox_tenant_status "
                    "ON sheets_outbox(tenant_id, status, created_at)"
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inventory_imports (
                        id VARCHAR PRIMARY KEY,
                        tenant_id VARCHAR NOT NULL,
                        idempotency_key VARCHAR NOT NULL,
                        content_hash VARCHAR NOT NULL,
                        created INTEGER NOT NULL DEFAULT 0 CHECK (created >= 0),
                        updated INTEGER NOT NULL DEFAULT 0 CHECK (updated >= 0),
                        skipped INTEGER NOT NULL DEFAULT 0 CHECK (skipped >= 0),
                        errors_json TEXT NOT NULL DEFAULT '[]',
                        created_at TIMESTAMPTZ NOT NULL,
                        UNIQUE (tenant_id, idempotency_key)
                    )
                    """
                )
                _seed_postgres_users(cursor)
                _seed_postgres_unassigned_location(cursor)
                if seed_demo_data():
                    _migrate_postgres_demo_inventory(cursor)
                    inserted_demo_skus = _seed_postgres_inventory(cursor)
                    _seed_postgres_locations(cursor)
                    _seed_postgres_demo_positions(cursor, inserted_demo_skus)
                _migrate_postgres_inventory_positions(cursor)
    finally:
        connection.close()


def _seed_postgres_users(cursor: Any) -> None:
    tenant = tenant_id()
    timestamp = utc_now()
    include_demo_badges = seed_demo_data()
    configured_users = [
        (
            "admin",
            os.getenv("STOCKPILE_ADMIN_USERNAME", "admin").strip().lower(),
            os.getenv("STOCKPILE_ADMIN_DISPLAY_NAME", "Alex Morgan").strip(),
            os.getenv("STOCKPILE_ADMIN_PASSWORD", ""),
            os.getenv(
                "STOCKPILE_ADMIN_BADGE_CODE",
                "ADM-1001" if include_demo_badges else "",
            ).strip(),
            os.getenv("STOCKPILE_ADMIN_BADGE_PIN", "").strip(),
        ),
        (
            "operator",
            os.getenv("STOCKPILE_OPERATOR_USERNAME", "operator").strip().lower(),
            os.getenv("STOCKPILE_OPERATOR_DISPLAY_NAME", "Jordan Lee").strip(),
            os.getenv("STOCKPILE_OPERATOR_PASSWORD", ""),
            os.getenv(
                "STOCKPILE_OPERATOR_BADGE_CODE",
                "OPS-2042" if include_demo_badges else "",
            ).strip(),
            os.getenv("STOCKPILE_OPERATOR_BADGE_PIN", "").strip(),
        ),
    ]
    for (
        role,
        username,
        display_name,
        password,
        badge_code,
        badge_pin,
    ) in configured_users:
        if not username or not password:
            continue
        badge_digest = badge_code_digest(badge_code, tenant) if badge_code else None
        badge_pin_hash = hash_badge_pin(badge_pin) if badge_pin else None
        cursor.execute(
            """
            INSERT INTO users
                (id, tenant_id, username, display_name, role, password_hash,
                 badge_code_digest, badge_pin_hash, active, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                username = EXCLUDED.username,
                display_name = EXCLUDED.display_name,
                role = EXCLUDED.role,
                password_hash = EXCLUDED.password_hash,
                badge_code_digest = COALESCE(
                    EXCLUDED.badge_code_digest,
                    users.badge_code_digest
                ),
                badge_pin_hash = CASE
                    WHEN EXCLUDED.badge_code_digest IS NOT NULL
                    THEN EXCLUDED.badge_pin_hash
                    ELSE users.badge_pin_hash
                END,
                active = TRUE
            """,
            (
                f"{tenant}-{role}",
                tenant,
                username,
                display_name or role.title(),
                role,
                hash_password(password),
                badge_digest,
                badge_pin_hash,
                timestamp,
            ),
        )


def _migrate_postgres_demo_inventory(cursor: Any) -> None:
    tenant = tenant_id()
    for sku, old_barcode, old_name, old_category in LEGACY_DEMO_INVENTORY:
        _, new_barcode, new_name, new_category, _ = DEMO_INVENTORY_BY_SKU[sku]
        cursor.execute(
            "SELECT 1 FROM inventory_transactions WHERE tenant_id = %s AND sku = %s LIMIT 1",
            (tenant, sku),
        )
        selected_barcode = old_barcode if cursor.fetchone() else new_barcode
        if selected_barcode != old_barcode:
            cursor.execute(
                "SELECT 1 FROM inventory WHERE tenant_id = %s AND barcode = %s AND sku <> %s LIMIT 1",
                (tenant, selected_barcode, sku),
            )
            if cursor.fetchone():
                selected_barcode = old_barcode

        cursor.execute(
            """
            UPDATE inventory
            SET barcode = %s, product_name = %s, category = %s
            WHERE tenant_id = %s AND sku = %s AND barcode = %s
              AND product_name = %s AND category = %s
            """,
            (
                selected_barcode,
                new_name,
                new_category,
                tenant,
                sku,
                old_barcode,
                old_name,
                old_category,
            ),
        )

def _seed_postgres_inventory(cursor: Any) -> set[str]:
    timestamp = utc_now()
    tenant = tenant_id()
    cursor.execute(
        "SELECT sku FROM inventory WHERE tenant_id = %s", (tenant,)
    )
    existing_demo_skus = {
        row["sku"] if isinstance(row, dict) else row[0]
        for row in cursor.fetchall()
        if (row["sku"] if isinstance(row, dict) else row[0])
        in DEMO_INVENTORY_BY_SKU
    }
    cursor.executemany(
        """
        INSERT INTO inventory
            (tenant_id, sku, barcode, product_name, category, current_stock,
             status, last_updated, version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
        ON CONFLICT DO NOTHING
        """,
        [
            (
                tenant,
                sku,
                barcode,
                name,
                category,
                stock,
                "in_stock" if stock > 0 else "out_of_stock",
                timestamp,
            )
            for sku, barcode, name, category, stock in DEMO_INVENTORY
        ],
    )
    return set(DEMO_INVENTORY_BY_SKU) - existing_demo_skus


def _seed_postgres_unassigned_location(cursor: Any) -> None:
    cursor.execute(
        """
        SELECT tenant_id FROM inventory
        UNION SELECT tenant_id FROM users
        UNION SELECT tenant_id FROM inventory_transactions
        """
    )
    tenants = {tenant_id()}
    tenants.update(
        row["tenant_id"] if isinstance(row, dict) else row[0]
        for row in cursor.fetchall()
    )
    code, name, kind, parent_code, stockable = UNASSIGNED_LOCATION
    cursor.executemany(
        """
        INSERT INTO locations
            (tenant_id, code, name, kind, parent_code, active, stockable, created_at)
        VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
        ON CONFLICT(tenant_id, code) DO UPDATE SET
            name = EXCLUDED.name,
            kind = EXCLUDED.kind,
            parent_code = EXCLUDED.parent_code,
            active = EXCLUDED.active,
            stockable = EXCLUDED.stockable
        """,
        [
            (tenant, code, name, kind, parent_code, stockable, utc_now())
            for tenant in tenants
        ],
    )


def _seed_postgres_locations(cursor: Any) -> None:
    tenant = tenant_id()
    timestamp = utc_now()
    cursor.executemany(
        """
        INSERT INTO locations
            (tenant_id, code, name, kind, parent_code, active, stockable, created_at)
        VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
        ON CONFLICT(tenant_id, code) DO UPDATE SET
            name = EXCLUDED.name,
            kind = EXCLUDED.kind,
            parent_code = EXCLUDED.parent_code,
            active = EXCLUDED.active,
            stockable = EXCLUDED.stockable
        """,
        [
            (tenant, code, name, kind, parent_code, stockable, timestamp)
            for code, name, kind, parent_code, stockable in DEMO_LOCATIONS
        ],
    )


def _seed_postgres_demo_positions(
    cursor: Any, inserted_skus: set[str]
) -> None:
    tenant = tenant_id()
    cursor.executemany(
        """
        INSERT INTO inventory_positions
            (tenant_id, sku, location_code, quantity, last_updated, version)
        VALUES (%s, %s, %s, %s, %s, 0)
        ON CONFLICT(tenant_id, sku, location_code) DO NOTHING
        """,
        [
            (tenant, sku, location_code, quantity, timestamp)
            for sku, location_code, quantity, timestamp
            in _demo_position_rows(inserted_skus, utc_now())
        ],
    )


def _migrate_postgres_inventory_positions(cursor: Any) -> None:
    cursor.execute(
        """
        INSERT INTO inventory_positions
            (tenant_id, sku, location_code, quantity, last_updated, version)
        SELECT i.tenant_id, i.sku, 'UNASSIGNED', i.current_stock,
               i.last_updated, 0
        FROM inventory AS i
        WHERE NOT EXISTS (
            SELECT 1
            FROM inventory_positions AS p
            WHERE p.tenant_id = i.tenant_id AND p.sku = i.sku
        )
        ON CONFLICT(tenant_id, sku, location_code) DO NOTHING
        """
    )


INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)
