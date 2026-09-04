from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from config import seed_demo_data, tenant_id
from database import (
    DEMO_INVENTORY_BY_SKU,
    badge_code_digest,
    execute_write,
    fetchone,
    get_db,
    init_db,
    utc_now,
)
from schemas import (
    InvestigationCreate,
    ProductUpdate,
    ReceiptCreate,
    ReceiptLine,
    ReservationClose,
    ReservationCreate,
    ReservationLine,
    SellingOrderImport,
    SellingOrderLine,
)
from services.inventory import (
    DomainError,
    close_reservation,
    create_investigation,
    create_order_comparison,
    create_receipt,
    create_reservation,
    update_product,
)
from security import hash_badge_pin, hash_password


DEMO_KEY_PREFIX = "stockpile-recording-v1"

# SKU: (barcode, unit, acquisition cost in centavos)
PRODUCT_METADATA: dict[str, tuple[str, str, int]] = {
    "PKG-BAG-M": ("2908170000012", "pack", 18_500),
    "BOX-060": ("2908170000029", "piece", 7_200),
    "TAPE-048": ("2908170000036", "pack", 19_800),
    "LBL-040": ("2908170000043", "roll", 31_500),
    "TAG-100": ("2908170000050", "pack", 14_500),
    "BAG-XL": ("2908170000067", "pack", 42_500),
    "TXT-POP-WHT-60": ("2908170000074", "roll", 395_000),
    "TXT-POP-BLK-60": ("2908170000081", "roll", 415_000),
    "TXT-BC-WHT-60": ("2908170000098", "roll", 365_000),
    "TXT-TWL-KHK-60": ("2908170000104", "roll", 520_000),
    "TXT-TWL-NVY-60": ("2908170000111", "roll", 535_000),
    "TXT-CNV-NAT-60": ("2908170000128", "roll", 610_000),
    "TXT-MUS-UNB-45": ("2908170000135", "roll", 315_000),
    "TXT-GNG-RBW-45": ("2908170000142", "roll", 470_000),
    "TXT-OXF-WHT-60": ("2908170000159", "roll", 495_000),
    "TXT-OXF-BLU-60": ("2908170000166", "roll", 510_000),
    "TXT-TC-WHT-60": ("2908170000173", "roll", 430_000),
    "TXT-GAB-NVY-60": ("2908170000180", "roll", 620_000),
    "TXT-GAB-BLK-60": ("2908170000197", "roll", 630_000),
    "TXT-PTW-KHK-60": ("2908170000203", "roll", 590_000),
    "TXT-JRS-BLK-60": ("2908170000210", "roll", 620_000),
    "TXT-JRS-WHT-60": ("2908170000227", "roll", 610_000),
    "TXT-RIB-GRY-45": ("2908170000234", "roll", 560_000),
    "TXT-SPX-BLK-60": ("2908170000241", "roll", 780_000),
    "TXT-FTR-GRY-60": ("2908170000258", "roll", 690_000),
    "TXT-MSH-WHT-60": ("2908170000265", "roll", 445_000),
    "TXT-SAT-CHP-60": ("2908170000272", "roll", 650_000),
    "TXT-SAT-BLK-60": ("2908170000289", "roll", 620_000),
    "TXT-CHF-WHT-60": ("2908170000296", "roll", 430_000),
    "TXT-ORG-IVO-60": ("2908170000302", "roll", 390_000),
    "TXT-TUL-WHT-72": ("2908170000319", "roll", 520_000),
    "TXT-LCE-IVO-60": ("2908170000326", "roll", 880_000),
    "TXT-LIN-BLK-60": ("2908170000333", "roll", 320_000),
    "TXT-LIN-WHT-60": ("2908170000340", "roll", 310_000),
    "TXT-LIN-BGE-60": ("2908170000357", "roll", 315_000),
    "TXT-FUS-LGT-60": ("2908170000364", "roll", 470_000),
    "TXT-FUS-MED-60": ("2908170000371", "roll", 495_000),
    "TXT-INT-NWV-60": ("2908170000388", "roll", 530_000),
    "NOT-THR-WHT-12": ("2908170000395", "box", 98_000),
    "NOT-THR-BLK-12": ("2908170000401", "box", 101_000),
}

REQUIRED_STORY_SKUS = {
    "TXT-POP-WHT-60",
    "TXT-POP-BLK-60",
    "TXT-OXF-BLU-60",
}

GENERIC_DISPLAY_NAMES = {
    "admin",
    "demo admin",
    "demo operator",
    "inventory admin",
    "operator",
    "warehouse operator",
}

RECORDING_DISPLAY_NAMES = {
    "admin": "Alex Morgan",
    "operator": "Jordan Lee",
}

SUPPORT_STAFF = {
    "receiving": {
        "username": "receiving-demo",
        "display_name": "Sam Patel",
        "badge_code": "RCV-3157",
        "badge_pin": "2648",
    },
    "counts": {
        "username": "counts-demo",
        "display_name": "Maya Chen",
        "badge_code": "CNT-4821",
        "badge_pin": "1357",
    },
}


def _active_user(db: Any, role: str) -> dict[str, Any] | None:
    user_id = f"{tenant_id()}-{role}"
    return fetchone(
        db,
        """
        SELECT id, tenant_id, username, display_name, role, active
        FROM users
        WHERE tenant_id = ? AND id = ? AND role = ? AND active = 1
        LIMIT 1
        """,
        (tenant_id(), user_id, role),
        """
        SELECT id, tenant_id, username, display_name, role, active
        FROM users
        WHERE tenant_id = %s AND id = %s AND role = %s AND active = TRUE
        LIMIT 1
        """,
    )


def _recording_display_name(role: str) -> str:
    environment_key = (
        "STOCKPILE_ADMIN_DISPLAY_NAME"
        if role == "admin"
        else "STOCKPILE_OPERATOR_DISPLAY_NAME"
    )
    configured = os.getenv(environment_key, "").strip()
    if configured and configured.casefold() not in GENERIC_DISPLAY_NAMES:
        return configured
    return RECORDING_DISPLAY_NAMES[role]


def _replace_generic_user_names(
    db: Any,
    users: dict[str, dict[str, Any]],
) -> None:
    changed = False
    for role, user in users.items():
        if user["display_name"].strip().casefold() not in GENERIC_DISPLAY_NAMES:
            continue
        display_name = _recording_display_name(role)
        execute_write(
            db,
            """
            UPDATE users SET display_name = ?
            WHERE tenant_id = ? AND id = ?
            """,
            (display_name, tenant_id(), user["id"]),
            """
            UPDATE users SET display_name = %s
            WHERE tenant_id = %s AND id = %s
            """,
        )
        user["display_name"] = display_name
        changed = True
    if changed:
        db.commit()


def _product(db: Any, sku: str) -> dict[str, Any] | None:
    return fetchone(
        db,
        """
        SELECT sku, barcode, product_name, category, unit,
               acquisition_cost_centavos, current_stock, active
        FROM inventory
        WHERE tenant_id = ? AND sku = ?
        """,
        (tenant_id(), sku),
        """
        SELECT sku, barcode, product_name, category, unit,
               acquisition_cost_centavos, current_stock, active
        FROM inventory
        WHERE tenant_id = %s AND sku = %s
        """,
    )


def _is_known_demo_product(item: dict[str, Any]) -> bool:
    expected = DEMO_INVENTORY_BY_SKU.get(item["sku"])
    if expected is None:
        return False
    _, barcode, product_name, category, _ = expected
    return (
        item["barcode"] == barcode
        and item["product_name"] == product_name
        and item["category"] == category
    )


def _staff_for_badge(db: Any, badge: str) -> dict[str, Any] | None:
    digest = badge_code_digest(badge, tenant_id())
    return fetchone(
        db,
        """
        SELECT id, tenant_id, username, display_name, role, active
        FROM users
        WHERE tenant_id = ? AND badge_code_digest = ? AND active = 1
        LIMIT 1
        """,
        (tenant_id(), digest),
        """
        SELECT id, tenant_id, username, display_name, role, active
        FROM users
        WHERE tenant_id = %s AND badge_code_digest = %s AND active = TRUE
        LIMIT 1
        """,
    )


def _seed_support_staff(db: Any) -> dict[str, dict[str, Any]]:
    """Create non-login staff identities used by the synthetic workday."""
    staff: dict[str, dict[str, Any]] = {}
    for key, record in SUPPORT_STAFF.items():
        user_id = f"{tenant_id()}-operator-{key}"
        params = (
            user_id,
            tenant_id(),
            record["username"],
            record["display_name"],
            hash_password(secrets.token_urlsafe(24)),
            badge_code_digest(record["badge_code"], tenant_id()),
            hash_badge_pin(record["badge_pin"]),
            utc_now(),
        )
        execute_write(
            db,
            """
            INSERT INTO users
                (id, tenant_id, username, display_name, role, password_hash,
                 badge_code_digest, badge_pin_hash, active, created_at)
            VALUES (?, ?, ?, ?, 'operator', ?, ?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                badge_code_digest = excluded.badge_code_digest,
                badge_pin_hash = excluded.badge_pin_hash,
                active = 1
            """,
            params,
            """
            INSERT INTO users
                (id, tenant_id, username, display_name, role, password_hash,
                 badge_code_digest, badge_pin_hash, active, created_at)
            VALUES (%s, %s, %s, %s, 'operator', %s, %s, %s, TRUE, %s)
            ON CONFLICT(id) DO UPDATE SET
                username = EXCLUDED.username,
                display_name = EXCLUDED.display_name,
                badge_code_digest = EXCLUDED.badge_code_digest,
                badge_pin_hash = EXCLUDED.badge_pin_hash,
                active = TRUE
            """,
        )
        staff[key] = {
            "id": user_id,
            "display_name": record["display_name"],
            "badge_code": record["badge_code"],
            "badge_pin": record["badge_pin"],
        }
    db.commit()
    return staff


def _location(db: Any, code: str) -> dict[str, Any] | None:
    return fetchone(
        db,
        """
        SELECT code, active, stockable
        FROM locations
        WHERE tenant_id = ? AND code = ?
        """,
        (tenant_id(), code),
        """
        SELECT code, active, stockable
        FROM locations
        WHERE tenant_id = %s AND code = %s
        """,
    )


def _position_total(db: Any, sku: str) -> int:
    row = fetchone(
        db,
        """
        SELECT COALESCE(SUM(quantity), 0) AS total
        FROM inventory_positions
        WHERE tenant_id = ? AND sku = ?
        """,
        (tenant_id(), sku),
        """
        SELECT COALESCE(SUM(quantity), 0) AS total
        FROM inventory_positions
        WHERE tenant_id = %s AND sku = %s
        """,
    )
    return int(row["total"] if row is not None else 0)


def _preflight_story(
    db: Any,
    staff: dict[str, Any],
    staff_badge: str,
) -> None:
    missing: list[str] = []
    incompatible: list[str] = []
    inactive: list[str] = []
    imbalanced: list[str] = []

    for sku in sorted(REQUIRED_STORY_SKUS):
        item = _product(db, sku)
        if item is None:
            missing.append(sku)
            continue
        if not _is_known_demo_product(item):
            incompatible.append(sku)
            continue
        if not bool(item["active"]):
            inactive.append(sku)
        if _position_total(db, sku) != int(item["current_stock"]):
            imbalanced.append(sku)

    if missing:
        raise RuntimeError(
            "The recording products are missing: " + ", ".join(missing)
        )
    if incompatible:
        raise RuntimeError(
            "A recording SKU is not owned by the demo dataset: "
            + ", ".join(incompatible)
        )
    if inactive:
        raise RuntimeError(
            "The recording products are archived: " + ", ".join(inactive)
        )
    if imbalanced:
        raise RuntimeError(
            "Product and location balances disagree for: " + ", ".join(imbalanced)
        )

    invalid_locations = []
    for code in ("WH-MNL-A01", "WH-MNL-A02", "WH-MNL-B01"):
        location = _location(db, code)
        if (
            location is None
            or not bool(location["active"])
            or not bool(location["stockable"])
        ):
            invalid_locations.append(code)
    if invalid_locations:
        raise RuntimeError(
            "The recording locations are unavailable: "
            + ", ".join(invalid_locations)
        )

    badge_user = _staff_for_badge(db, staff_badge)
    if badge_user is None or badge_user["id"] != staff["id"]:
        raise RuntimeError(
            "STOCKPILE_OPERATOR_BADGE_CODE does not belong to the configured staff account."
        )


def _enrich_products(db: Any, owner: dict[str, Any]) -> tuple[int, list[str]]:
    updated = 0
    incompatible: list[str] = []

    for sku, (barcode, desired_unit, desired_cost) in PRODUCT_METADATA.items():
        item = _product(db, sku)
        if item is None:
            continue
        if item["barcode"] != barcode or not _is_known_demo_product(item):
            incompatible.append(sku)
            continue

        changes: dict[str, Any] = {}
        current_unit = str(item.get("unit") or "").strip().casefold()
        if current_unit in {"", "unit"}:
            changes["unit"] = desired_unit
        if item.get("acquisition_cost_centavos") is None:
            changes["acquisition_cost_centavos"] = desired_cost

        if changes:
            update_product(
                db,
                owner,
                sku,
                ProductUpdate(**changes),
            )
            updated += 1

    return updated, incompatible


def _stagger_demo_product_dates(db: Any) -> int:
    """Give untouched synthetic catalog rows a believable recent timeline."""
    changed = 0
    now = datetime.now(timezone.utc)
    for index, sku in enumerate(sorted(PRODUCT_METADATA)):
        if sku in REQUIRED_STORY_SKUS:
            continue
        timestamp = (
            now
            - timedelta(
                days=1 + (index * 5) % 17,
                hours=(index * 3) % 8,
            )
        ).isoformat()
        rowcount = execute_write(
            db,
            """
            UPDATE inventory SET last_updated = ?
            WHERE tenant_id = ? AND sku = ? AND version <= 1
            """,
            (timestamp, tenant_id(), sku),
            """
            UPDATE inventory SET last_updated = %s
            WHERE tenant_id = %s AND sku = %s AND version <= 1
            """,
        )
        changed += max(0, int(rowcount or 0))
    db.commit()
    return changed


def _staff_badge() -> tuple[str, str | None]:
    badge = os.getenv(
        "STOCKPILE_OPERATOR_BADGE_CODE",
        "OPS-2042",
    ).strip()
    pin = os.getenv(
        "STOCKPILE_OPERATOR_BADGE_PIN",
        "",
    ).strip()
    if not badge:
        raise RuntimeError(
            "Configure STOCKPILE_OPERATOR_BADGE_CODE before seeding the recording demo."
        )
    return badge, pin or None


def _seed_delivery(
    db: Any,
    owner: dict[str, Any],
    staff_badge: str,
    staff_pin: str | None,
) -> dict[str, Any]:
    return create_receipt(
        db,
        owner,
        ReceiptCreate(
            supplier_name="San Nicolas Textile Supply",
            reference="DR-1847",
            source="scanner",
            receiver_badge_code=staff_badge,
            receiver_pin=staff_pin,
            note="Morning delivery counted at receiving.",
            idempotency_key=f"{DEMO_KEY_PREFIX}:delivery",
            lines=[
                ReceiptLine(
                    barcode="2908170000074",
                    quantity=18,
                    location_code="WH-MNL-A01",
                ),
                ReceiptLine(
                    barcode="2908170000081",
                    quantity=12,
                    location_code="WH-MNL-A02",
                ),
                ReceiptLine(
                    barcode="2908170000166",
                    quantity=10,
                    location_code="WH-MNL-B01",
                ),
            ],
        ),
    )


def _seed_fulfilled_order(
    db: Any,
    owner: dict[str, Any],
    staff_badge: str,
    staff_pin: str | None,
) -> dict[str, Any]:
    created = create_reservation(
        db,
        owner,
        ReservationCreate(
            reference="SP-10427",
            customer_name="Northline Uniforms",
            sales_channel="Shopee",
            note="Two white poplin rolls reserved for dispatch.",
            idempotency_key=f"{DEMO_KEY_PREFIX}:reservation",
            lines=[
                ReservationLine(
                    barcode="2908170000074",
                    location_code="WH-MNL-A01",
                    quantity=2,
                )
            ],
        ),
    )
    reservation_id = created["data"]["id"]
    return close_reservation(
        db,
        owner,
        reservation_id,
        ReservationClose(
            action="fulfill",
            employee_badge_code=staff_badge,
            employee_pin=staff_pin,
            reason="Released to dispatch after staff count.",
            idempotency_key=f"{DEMO_KEY_PREFIX}:reservation-fulfill",
        ),
    )


def _seed_order_check(
    db: Any,
    owner: dict[str, Any],
) -> dict[str, Any]:
    return create_order_comparison(
        db,
        owner,
        SellingOrderImport(
            sales_channel="Shopee",
            idempotency_key=f"{DEMO_KEY_PREFIX}:order-check",
            lines=[
                SellingOrderLine(
                    order_reference="SP-10427",
                    sku="TXT-POP-WHT-60",
                    quantity=2,
                ),
                SellingOrderLine(
                    order_reference="SP-10428",
                    sku="TXT-OXF-BLU-60",
                    quantity=3,
                ),
            ],
        ),
        source_filename="shopee-orders-recording.csv",
        source_row_numbers=[2, 3],
    )


def _seed_investigation(
    db: Any,
    owner: dict[str, Any],
    staff_badge: str,
    staff_pin: str | None,
) -> dict[str, Any]:
    return create_investigation(
        db,
        owner,
        InvestigationCreate(
            kind="misplaced",
            barcode="2908170000081",
            quantity=1,
            expected_location_code="WH-MNL-A01",
            observed_location_code="WH-MNL-B01",
            reference="COUNT-A-0829",
            employee_badge_code=staff_badge,
            employee_pin=staff_pin,
            note="One black poplin roll was found in Rack B during the morning count.",
            idempotency_key=f"{DEMO_KEY_PREFIX}:investigation",
        ),
    )


def seed_recording_demo(db: Any) -> dict[str, Any]:
    if not seed_demo_data():
        raise RuntimeError(
            "Set STOCKPILE_SEED_DEMO_DATA=true before seeding recording data."
        )

    owner = _active_user(db, "admin")
    staff = _active_user(db, "operator")
    if owner is None or staff is None:
        raise RuntimeError(
            "Create both owner and staff accounts before seeding the recording demo."
        )

    staff_badge, staff_pin = _staff_badge()
    _preflight_story(db, staff, staff_badge)

    users = {"admin": owner, "operator": staff}
    _replace_generic_user_names(db, users)

    support_staff = _seed_support_staff(db)

    delivery = _seed_delivery(
        db,
        owner,
        support_staff["receiving"]["badge_code"],
        support_staff["receiving"]["badge_pin"],
    )
    fulfilled = _seed_fulfilled_order(db, owner, staff_badge, staff_pin)
    order_check = _seed_order_check(db, owner)
    investigation = _seed_investigation(
        db,
        owner,
        support_staff["counts"]["badge_code"],
        support_staff["counts"]["badge_pin"],
    )
    updated, incompatible = _enrich_products(db, owner)
    dated = _stagger_demo_product_dates(db)

    return {
        "owner": owner["display_name"],
        "staff": [
            staff["display_name"],
            support_staff["receiving"]["display_name"],
            support_staff["counts"]["display_name"],
        ],
        "generic_names": [
            name
            for name in (owner["display_name"], staff["display_name"])
            if name.strip().casefold() in GENERIC_DISPLAY_NAMES
        ],
        "products_updated": updated,
        "products_skipped": len(incompatible),
        "product_dates_staggered": dated,
        "delivery_replayed": bool(delivery.get("replayed")),
        "fulfillment_replayed": bool(fulfilled.get("replayed")),
        "order_check_replayed": bool(order_check.get("replayed")),
        "investigation_replayed": bool(investigation.get("replayed")),
    }


def _connection() -> tuple[Iterator[Any], Any]:
    generator = get_db()
    return generator, next(generator)


def main() -> None:
    if not seed_demo_data():
        raise SystemExit(
            "Set STOCKPILE_SEED_DEMO_DATA=true before running this recording seed."
        )

    init_db()
    generator, db = _connection()
    try:
        result = seed_recording_demo(db)
    except DomainError as error:
        raise SystemExit(f"Demo seed failed: {error.detail}") from error
    except RuntimeError as error:
        raise SystemExit(f"Demo seed failed: {error}") from error
    finally:
        generator.close()

    print("Stockpile recording data is ready.")
    print(f"Owner: {result['owner']}")
    print("Staff: " + ", ".join(result["staff"]))
    print(f"Product records enriched: {result['products_updated']}")
    print(f"Product dates staggered: {result['product_dates_staggered']}")
    if result["products_skipped"]:
        print(
            "Non-demo product records left unchanged: "
            f"{result['products_skipped']}"
        )
    if result["generic_names"]:
        print(
            "Set STOCKPILE_ADMIN_DISPLAY_NAME and "
            "STOCKPILE_OPERATOR_DISPLAY_NAME to replace generic account names."
        )
    replayed = [
        label
        for label, key in (
            ("delivery", "delivery_replayed"),
            ("fulfilled order", "fulfillment_replayed"),
            ("order comparison", "order_check_replayed"),
            ("investigation", "investigation_replayed"),
        )
        if result[key]
    ]
    if replayed:
        print("Existing demo records reused: " + ", ".join(replayed))


if __name__ == "__main__":
    main()
