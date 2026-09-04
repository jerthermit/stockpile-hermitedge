import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from random import Random
from threading import Event

import pytest
from fastapi.testclient import TestClient

from config import validate_runtime_config
from database import init_db
from main import app
from security import hash_badge_pin, verify_password
from services.sheets import SHEET_HEADERS, _sheet_value, _transaction_sheet_row


BARCODE = "2908170000012"
LOCATION_CODE = "WH-MNL-D01"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database_path = tmp_path / "stockpile-test.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("STOCKPILE_DB_PATH", str(database_path))
    monkeypatch.setenv("STOCKPILE_ENV", "test")
    monkeypatch.setenv("STOCKPILE_AUTH_SECRET", "test-signing-secret-with-more-than-32-characters")
    monkeypatch.setenv("STOCKPILE_TENANT_ID", "test-tenant")
    monkeypatch.setenv("STOCKPILE_SEED_DEMO_DATA", "true")
    monkeypatch.setenv("STOCKPILE_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("STOCKPILE_ADMIN_PASSWORD", "admin-password-123")
    monkeypatch.setenv("STOCKPILE_OPERATOR_USERNAME", "operator")
    monkeypatch.setenv("STOCKPILE_OPERATOR_PASSWORD", "operator-password-123")
    monkeypatch.setenv("STOCKPILE_OPERATOR_DISPLAY_NAME", "Warehouse Operator")
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)

    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.database_path = database_path
        yield test_client


def login(client: TestClient, username: str = "operator", password: str | None = None):
    password = password or f"{username}-password-123"
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def transaction_payload(**overrides):
    payload = {
        "barcode": BARCODE,
        "movement_type": "stock_out",
        "quantity": 2,
        "location_code": LOCATION_CODE,
        "reason": "Customer order",
        "idempotency_key": str(uuid.uuid4()),
        "source": "scanner",
    }
    payload.update(overrides)
    return payload


def get_stock(client: TestClient, headers: dict[str, str]) -> int:
    response = client.get(f"/api/v1/inventory/barcodes/{BARCODE}", headers=headers)
    assert response.status_code == 200
    return response.json()["data"]["current_stock"]


def get_item(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.get(f"/api/v1/inventory/barcodes/{BARCODE}", headers=headers)
    assert response.status_code == 200
    return response.json()["data"]


def position_quantity(item: dict, location_code: str) -> int:
    return next(
        (
            position["quantity"]
            for position in item["positions"]
            if position["location_code"] == location_code
        ),
        0,
    )


def test_badge_pin_hash_accepts_four_digits_and_rejects_invalid_values():
    encoded = hash_badge_pin("4826")

    assert verify_password("4826", encoded)
    assert not verify_password("4827", encoded)
    with pytest.raises(ValueError, match="4 to 12 digits"):
        hash_badge_pin("123")
    with pytest.raises(ValueError, match="4 to 12 digits"):
        hash_badge_pin("badge-pin")


def test_inventory_requires_authentication(client):
    assert client.get("/api/v1/inventory").status_code == 401
    assert (
        client.get(
            "/api/v1/inventory", headers={"Authorization": "Bearer malformed"}
        ).status_code
        == 401
    )


def test_cors_allows_ai_and_product_management_preflights(client):
    cases = (
        (
            "/api/v1/inventory/ai/discrepancies",
            "POST",
            "authorization,idempotency-key",
        ),
        (
            "/api/v1/inventory/ai/delivery-document",
            "POST",
            "authorization,content-type,idempotency-key,x-stockpile-filename",
        ),
        (
            "/api/v1/inventory/products/PKG-BAG-M/image",
            "PUT",
            "authorization,content-type",
        ),
        (
            "/api/v1/inventory/products/PKG-BAG-M",
            "PATCH",
            "authorization,content-type",
        ),
        (
            "/api/v1/inventory/products/PKG-BAG-M/image",
            "DELETE",
            "authorization",
        ),
    )

    for path, method, requested_headers in cases:
        response = client.options(
            path,
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": requested_headers,
            },
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == (
            "http://localhost:3000"
        )


def test_product_image_upload_accepts_supported_file_and_rejects_over_10_mb(
    client, monkeypatch
):
    from PIL import Image

    image_directory = client.database_path.parent / "product-images"
    monkeypatch.setenv("STOCKPILE_PRODUCT_IMAGE_DIR", str(image_directory))

    source = Image.frombytes(
        "RGB",
        (256, 256),
        Random(0).randbytes(256 * 256 * 3),
    )
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    contents = buffer.getvalue()
    assert 50 * 1024 < len(contents) < 10_000_000

    response = client.put(
        "/api/v1/inventory/products/TXT-SAT-BLK-60/image",
        headers=login(client, "admin"),
        files={"file": ("black-satin.png", contents, "image/png")},
    )

    assert response.status_code == 200
    item = response.json()["data"]
    assert item["image_url"].startswith(
        "/api/v1/inventory/products/TXT-SAT-BLK-60/image/"
    )
    assert item["image_url"].endswith(".webp")

    oversized = client.put(
        "/api/v1/inventory/products/TXT-SAT-BLK-60/image",
        headers=login(client, "admin"),
        files={"file": ("too-large.png", b"x" * 10_000_001, "image/png")},
    )

    assert oversized.status_code == 413
    assert oversized.json()["detail"]["code"] == "product_image_too_large"


def test_demo_catalog_is_contextualized_for_textile_operations(client):
    response = client.get("/api/v1/inventory", headers=login(client))
    assert response.status_code == 200
    items = response.json()["data"]
    assert len(items) == 40
    assert {item["category"] for item in items} >= {
        "Cotton Wovens",
        "Uniform & Shirting",
        "Knits & Stretch",
        "Formal & Occasion",
    }
    assert any("50 m roll" in item["product_name"] for item in items)


def test_locations_and_inventory_positions_are_exposed_and_balanced(client):
    headers = login(client)
    location_response = client.get("/api/v1/inventory/locations", headers=headers)
    inventory_response = client.get("/api/v1/inventory", headers=headers)

    assert location_response.status_code == 200
    locations = location_response.json()["data"]
    rack = next(location for location in locations if location["code"] == LOCATION_CODE)
    assert rack == {
        "code": LOCATION_CODE,
        "name": "Rack D · Shelf 01 · Bin 01",
        "kind": "bin",
        "parent_code": "WH-MNL-SUP",
        "path": "Warehouse 01 / Packing & Notions / Rack D · Shelf 01 · Bin 01",
        "active": True,
        "stockable": True,
    }

    assert inventory_response.status_code == 200
    for item in inventory_response.json()["data"]:
        assert sum(position["quantity"] for position in item["positions"]) == item[
            "current_stock"
        ]


def test_legacy_demo_catalog_migration_preserves_history_and_custom_data(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "legacy-demo.db"
    tenant = "migration-tenant"
    legacy_timestamp = "2026-08-20T08:00:00+00:00"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("STOCKPILE_DB_PATH", str(database_path))
    monkeypatch.setenv("STOCKPILE_ENV", "test")
    monkeypatch.setenv("STOCKPILE_TENANT_ID", tenant)
    monkeypatch.setenv("STOCKPILE_SEED_DEMO_DATA", "true")
    monkeypatch.setenv("STOCKPILE_OPERATOR_USERNAME", "operator")
    monkeypatch.setenv("STOCKPILE_OPERATOR_PASSWORD", "operator-password-123")
    monkeypatch.setenv("STOCKPILE_OPERATOR_DISPLAY_NAME", "Demo Operator")
    monkeypatch.delenv("STOCKPILE_ADMIN_PASSWORD", raising=False)
    init_db()

    transactions = [
        (
            "legacy-stock-in",
            tenant,
            "legacy-stock-in-key",
            "BAG-XL",
            "4800000000066",
            "stock_in",
            120,
            120,
            120,
            240,
            f"{tenant}-operator",
            "Divisoria Demo Operator",
            "Legacy receiving test",
            "scanner",
            "2026-08-20T08:05:00+00:00",
        ),
        (
            "legacy-stock-out",
            tenant,
            "legacy-stock-out-key",
            "BAG-XL",
            "4800000000066",
            "stock_out",
            240,
            -240,
            240,
            0,
            f"{tenant}-operator",
            "Divisoria Demo Operator",
            "Legacy dispatch test",
            "scanner",
            "2026-08-20T08:10:00+00:00",
        ),
    ]
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE inventory
            SET barcode = '4800000000011',
                product_name = 'Resealable Bags, Medium',
                category = 'Packaging'
            WHERE tenant_id = ? AND sku = 'PKG-BAG-M'
            """,
            (tenant,),
        )
        connection.execute(
            """
            UPDATE inventory
            SET barcode = '4800000000066',
                product_name = 'Carry Bags, Extra Large',
                category = 'Packaging', current_stock = 0,
                status = 'out_of_stock', last_updated = ?, version = 2
            WHERE tenant_id = ? AND sku = 'BAG-XL'
            """,
            (legacy_timestamp, tenant),
        )
        connection.execute(
            """
            INSERT INTO inventory
                (tenant_id, sku, barcode, product_name, category, current_stock,
                 status, last_updated, version)
            VALUES (?, 'USER-CUSTOM', 'CUSTOM-9001', 'Custom Client Item',
                    'Client Catalog', 7, 'in_stock', ?, 4)
            """,
            (tenant, legacy_timestamp),
        )
        connection.executemany(
            """
            INSERT INTO inventory_transactions
                (id, tenant_id, idempotency_key, sku, barcode, movement_type,
                 quantity, quantity_delta, balance_before, balance_after,
                 actor_user_id, actor_name, reason, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            transactions,
        )
        connection.executemany(
            """
            INSERT INTO sheets_outbox
                (id, tenant_id, transaction_id, status, attempts, created_at)
            VALUES (?, ?, ?, 'not_configured', 0, ?)
            """,
            [
                ("legacy-outbox-in", tenant, "legacy-stock-in", transactions[0][-1]),
                ("legacy-outbox-out", tenant, "legacy-stock-out", transactions[1][-1]),
            ],
        )

    init_db()
    with sqlite3.connect(database_path) as connection:
        inventory_after_first_run = connection.execute(
            """
            SELECT sku, barcode, product_name, category, current_stock, status,
                   last_updated, version
            FROM inventory WHERE tenant_id = ? ORDER BY sku
            """,
            (tenant,),
        ).fetchall()
        history_after_first_run = connection.execute(
            """
            SELECT id, tenant_id, idempotency_key, sku, barcode, movement_type,
                   quantity, quantity_delta, balance_before, balance_after,
                   actor_user_id, actor_name, reason, source, created_at
            FROM inventory_transactions WHERE tenant_id = ? ORDER BY created_at
            """,
            (tenant,),
        ).fetchall()

    init_db()
    with sqlite3.connect(database_path) as connection:
        inventory_after_second_run = connection.execute(
            """
            SELECT sku, barcode, product_name, category, current_stock, status,
                   last_updated, version
            FROM inventory WHERE tenant_id = ? ORDER BY sku
            """,
            (tenant,),
        ).fetchall()
        bag = connection.execute(
            """
            SELECT barcode, product_name, category, current_stock, status,
                   last_updated, version
            FROM inventory WHERE tenant_id = ? AND sku = 'BAG-XL'
            """,
            (tenant,),
        ).fetchone()
        package = connection.execute(
            """
            SELECT barcode, product_name, category
            FROM inventory WHERE tenant_id = ? AND sku = 'PKG-BAG-M'
            """,
            (tenant,),
        ).fetchone()
        custom = connection.execute(
            """
            SELECT barcode, product_name, category, current_stock, status,
                   last_updated, version
            FROM inventory WHERE tenant_id = ? AND sku = 'USER-CUSTOM'
            """,
            (tenant,),
        ).fetchone()
        joined_history_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM inventory_transactions AS transaction_record
            JOIN inventory AS item
              ON item.tenant_id = transaction_record.tenant_id
             AND item.sku = transaction_record.sku
            JOIN sheets_outbox AS exchange
              ON exchange.transaction_id = transaction_record.id
            WHERE transaction_record.tenant_id = ?
              AND transaction_record.sku = 'BAG-XL'
            """,
            (tenant,),
        ).fetchone()[0]
        history_after_second_run = connection.execute(
            """
            SELECT id, tenant_id, idempotency_key, sku, barcode, movement_type,
                   quantity, quantity_delta, balance_before, balance_after,
                   actor_user_id, actor_name, reason, source, created_at
            FROM inventory_transactions WHERE tenant_id = ? ORDER BY created_at
            """,
            (tenant,),
        ).fetchall()

    assert len(inventory_after_second_run) == 41
    assert inventory_after_second_run == inventory_after_first_run
    assert bag == (
        "4800000000066",
        "Clear Garment Bag 24 × 40 in · Pack of 50",
        "Packing & Labels",
        0,
        "out_of_stock",
        legacy_timestamp,
        2,
    )
    assert package == (
        "2908170000012",
        "Clear Poly Bag 12 × 18 in · Pack of 100",
        "Packing & Labels",
    )
    assert custom == (
        "CUSTOM-9001",
        "Custom Client Item",
        "Client Catalog",
        7,
        "in_stock",
        legacy_timestamp,
        4,
    )
    assert joined_history_count == 2
    assert history_after_first_run == transactions
    assert history_after_second_run == transactions


def test_existing_aggregate_stock_migrates_to_unassigned_once(tmp_path, monkeypatch):
    database_path = tmp_path / "aggregate-only.db"
    tenant = "aggregate-tenant"
    timestamp = "2026-08-20T08:00:00+00:00"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("STOCKPILE_DB_PATH", str(database_path))
    monkeypatch.setenv("STOCKPILE_ENV", "test")
    monkeypatch.setenv("STOCKPILE_TENANT_ID", tenant)
    monkeypatch.setenv("STOCKPILE_SEED_DEMO_DATA", "false")
    monkeypatch.delenv("STOCKPILE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("STOCKPILE_OPERATOR_PASSWORD", raising=False)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE inventory (
                tenant_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                barcode TEXT,
                product_name TEXT NOT NULL,
                category TEXT NOT NULL,
                current_stock INTEGER NOT NULL,
                status TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (tenant_id, sku)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO inventory
                (tenant_id, sku, barcode, product_name, category, current_stock,
                 status, last_updated, version)
            VALUES (?, 'LEGACY-SKU', 'LEGACY-BARCODE', 'Legacy item', 'Legacy',
                    7, 'in_stock', ?, 0)
            """,
            (tenant, timestamp),
        )

    init_db()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT location_code, quantity FROM inventory_positions
            WHERE tenant_id = ? AND sku = 'LEGACY-SKU'
            """,
            (tenant,),
        ).fetchall() == [("UNASSIGNED", 7)]
        connection.execute(
            """
            INSERT INTO locations
                (tenant_id, code, name, kind, parent_code, active, stockable, created_at)
            VALUES (?, 'LEGACY-RACK', 'Legacy rack', 'rack', NULL, 1, 1, ?)
            """,
            (tenant, timestamp),
        )
        connection.execute(
            """
            UPDATE inventory_positions SET quantity = 2
            WHERE tenant_id = ? AND sku = 'LEGACY-SKU'
              AND location_code = 'UNASSIGNED'
            """,
            (tenant,),
        )
        connection.execute(
            """
            INSERT INTO inventory_positions
                (tenant_id, sku, location_code, quantity, last_updated, version)
            VALUES (?, 'LEGACY-SKU', 'LEGACY-RACK', 5, ?, 0)
            """,
            (tenant, timestamp),
        )

    init_db()
    with sqlite3.connect(database_path) as connection:
        positions = connection.execute(
            """
            SELECT location_code, quantity FROM inventory_positions
            WHERE tenant_id = ? AND sku = 'LEGACY-SKU'
            ORDER BY location_code
            """,
            (tenant,),
        ).fetchall()
    assert positions == [("LEGACY-RACK", 5), ("UNASSIGNED", 2)]


def test_login_returns_role_and_rejects_bad_password(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": "operator-password-123"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "operator"
    assert "tenant_id" not in response.json()["user"]

    rejected = client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": "wrong-password"},
    )
    assert rejected.status_code == 401


def test_valid_transaction_changes_stock_once_and_duplicate_replays(client):
    headers = login(client)
    before = get_stock(client, headers)
    payload = transaction_payload()

    created = client.post(
        "/api/v1/inventory/transactions", json=payload, headers=headers
    )
    replayed = client.post(
        "/api/v1/inventory/transactions", json=payload, headers=headers
    )

    assert created.status_code == 200
    assert created.json()["replayed"] is False
    assert created.json()["authority"] == "Stockpile application database"
    assert created.json()["data"]["actor_name"] == "Warehouse Operator"
    assert created.json()["data"]["balance_before"] == before
    assert created.json()["data"]["balance_after"] == before - 2
    assert created.json()["data"]["location_code"] == LOCATION_CODE
    assert created.json()["data"]["location_balance_after"] == (
        created.json()["data"]["location_balance_before"] - 2
    )
    assert created.json()["exchange"]["status"] == "not_configured"
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert replayed.json()["data"]["id"] == created.json()["data"]["id"]
    assert get_stock(client, headers) == before - 2


def test_same_idempotency_key_cannot_change_payload(client):
    headers = login(client)
    payload = transaction_payload()
    assert (
        client.post("/api/v1/inventory/transactions", json=payload, headers=headers).status_code
        == 200
    )

    payload["quantity"] = 3
    response = client.post(
        "/api/v1/inventory/transactions", json=payload, headers=headers
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "idempotency_conflict"

    payload["quantity"] = 2
    payload["location_code"] = "WH-MNL-D02"
    location_conflict = client.post(
        "/api/v1/inventory/transactions", json=payload, headers=headers
    )
    assert location_conflict.status_code == 409
    assert location_conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_simultaneous_duplicate_submissions_change_stock_once(client):
    headers = login(client)
    before = get_stock(client, headers)
    payload = transaction_payload()

    def submit_once(_):
        return client.post(
            "/api/v1/inventory/transactions", json=payload, headers=headers
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(submit_once, range(8)))

    assert all(response.status_code == 200 for response in responses)
    assert sum(response.json()["replayed"] is False for response in responses) == 1
    assert len({response.json()["data"]["id"] for response in responses}) == 1
    assert get_stock(client, headers) == before - 2
    history = client.get("/api/v1/inventory/transactions", headers=headers)
    assert len(history.json()["data"]) == 1


def test_unknown_barcode_invalid_quantity_and_negative_stock_are_rejected(client):
    headers = login(client)
    before = get_stock(client, headers)

    unknown = client.get(
        "/api/v1/inventory/barcodes/does-not-exist", headers=headers
    )
    invalid_responses = [
        client.post(
            "/api/v1/inventory/transactions",
            json=transaction_payload(quantity=quantity),
            headers=headers,
        )
        for quantity in (0, -1, True, 1.5, "2")
    ]
    negative = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(quantity=before + 1),
        headers=headers,
    )

    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "unknown_barcode"
    assert all(response.status_code == 422 for response in invalid_responses)
    assert negative.status_code == 409
    assert negative.json()["detail"]["code"] == "insufficient_available_stock"
    assert get_stock(client, headers) == before


def test_stock_out_is_limited_by_selected_location(client):
    headers = login(client)
    before = get_item(client, headers)
    selected_quantity = position_quantity(before, "WH-MNL-D02")
    assert before["current_stock"] > selected_quantity + 1

    response = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(
            location_code="WH-MNL-D02",
            quantity=selected_quantity + 1,
        ),
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "insufficient_available_stock"
    after = get_item(client, headers)
    assert after["current_stock"] == before["current_stock"]
    assert position_quantity(after, "WH-MNL-D02") == selected_quantity


def test_stock_in_updates_one_position_total_history_and_exchange(client):
    headers = login(client)
    before = get_item(client, headers)
    receiving_before = position_quantity(before, "WH-MNL-REC")

    response = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(
            movement_type="stock_in",
            quantity=5,
            location_code="WH-MNL-REC",
        ),
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["location_code"] == "WH-MNL-REC"
    assert body["data"]["location_balance_before"] == receiving_before
    assert body["data"]["location_balance_after"] == receiving_before + 5
    assert body["data"]["balance_after"] == before["current_stock"] + 5
    assert position_quantity(body["inventory"], "WH-MNL-REC") == receiving_before + 5
    assert sum(position["quantity"] for position in body["inventory"]["positions"]) == body[
        "inventory"
    ]["current_stock"]

    history = client.get("/api/v1/inventory/transactions", headers=headers).json()[
        "data"
    ]
    assert history[0]["location_code"] == "WH-MNL-REC"
    exchange = client.get("/api/v1/inventory/exchange", headers=headers).json()[
        "data"
    ]
    assert exchange[0]["location_code"] == "WH-MNL-REC"


def test_non_stockable_location_is_rejected_without_changes(client):
    headers = login(client)
    before = get_stock(client, headers)
    response = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(
            movement_type="stock_in",
            location_code="WH-MNL",
        ),
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "location_not_stockable"
    assert get_stock(client, headers) == before


def test_admin_reversal_preserves_original_history_and_is_idempotent(client):
    operator_headers = login(client)
    admin_headers = login(client, "admin")
    before = get_stock(client, operator_headers)
    created = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(quantity=3),
        headers=operator_headers,
    ).json()
    transaction_id = created["data"]["id"]
    reversal_payload = {
        "reason": "Operator entered the wrong quantity",
        "idempotency_key": str(uuid.uuid4()),
    }

    forbidden = client.post(
        f"/api/v1/inventory/transactions/{transaction_id}/reverse",
        json=reversal_payload,
        headers=operator_headers,
    )
    reversed_response = client.post(
        f"/api/v1/inventory/transactions/{transaction_id}/reverse",
        json=reversal_payload,
        headers=admin_headers,
    )
    replayed = client.post(
        f"/api/v1/inventory/transactions/{transaction_id}/reverse",
        json=reversal_payload,
        headers=admin_headers,
    )

    assert forbidden.status_code == 403
    assert reversed_response.status_code == 200
    assert reversed_response.json()["data"]["reverses_transaction_id"] == transaction_id
    assert reversed_response.json()["data"]["location_code"] == LOCATION_CODE
    assert reversed_response.json()["data"]["location_balance_after"] == created[
        "data"
    ]["location_balance_before"]
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert get_stock(client, admin_headers) == before

    history = client.get(
        "/api/v1/inventory/transactions", headers=admin_headers
    ).json()["data"]
    assert len(history) == 2
    original = next(item for item in history if item["id"] == transaction_id)
    assert original["reversed_by_transaction_id"] == reversed_response.json()["data"]["id"]


def test_reversal_idempotency_includes_reason(client):
    operator_headers = login(client)
    admin_headers = login(client, "admin")
    created = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(),
        headers=operator_headers,
    ).json()
    payload = {"reason": "First correction reason", "idempotency_key": str(uuid.uuid4())}
    path = f"/api/v1/inventory/transactions/{created['data']['id']}/reverse"
    assert client.post(path, json=payload, headers=admin_headers).status_code == 200

    payload["reason"] = "Changed correction reason"
    conflict = client.post(path, json=payload, headers=admin_headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_database_failure_rolls_back_inventory_and_history(client):
    headers = login(client)
    before = get_stock(client, headers)
    connection = sqlite3.connect(client.database_path)
    connection.execute(
        """
        CREATE TRIGGER force_transaction_failure
        BEFORE INSERT ON sheets_outbox
        BEGIN
            SELECT RAISE(ABORT, 'forced transaction failure');
        END
        """
    )
    connection.commit()
    connection.close()

    response = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(),
        headers=headers,
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "database_write_failed"
    assert get_stock(client, headers) == before
    history = client.get("/api/v1/inventory/transactions", headers=headers)
    assert history.status_code == 200
    assert history.json()["data"] == []


def test_production_requires_persistent_secret_and_disables_demo_seed(monkeypatch):
    monkeypatch.setenv("STOCKPILE_ENV", "production")
    monkeypatch.delenv("STOCKPILE_AUTH_SECRET", raising=False)
    monkeypatch.setenv("STOCKPILE_SEED_DEMO_DATA", "false")
    with pytest.raises(RuntimeError, match="STOCKPILE_AUTH_SECRET"):
        validate_runtime_config()

    monkeypatch.setenv(
        "STOCKPILE_AUTH_SECRET", "production-signing-secret-with-more-than-32-characters"
    )
    monkeypatch.setenv("STOCKPILE_SEED_DEMO_DATA", "true")
    with pytest.raises(RuntimeError, match="STOCKPILE_SEED_DEMO_DATA"):
        validate_runtime_config()

    monkeypatch.setenv("STOCKPILE_SEED_DEMO_DATA", "false")
    monkeypatch.setenv(
        "STOCKPILE_AUTH_SECRET", "replace-with-a-long-random-secret"
    )
    with pytest.raises(RuntimeError, match="example STOCKPILE_AUTH_SECRET"):
        validate_runtime_config()

    monkeypatch.setenv(
        "STOCKPILE_AUTH_SECRET", "production-signing-secret-with-more-than-32-characters"
    )
    monkeypatch.setenv(
        "STOCKPILE_ADMIN_PASSWORD", "replace-with-admin-password"
    )
    with pytest.raises(RuntimeError, match="example Stockpile account passwords"):
        validate_runtime_config()


def test_configured_username_can_change_without_replacing_user_identity(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "renamed-user.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("STOCKPILE_DB_PATH", str(database_path))
    monkeypatch.setenv("STOCKPILE_TENANT_ID", "rename-tenant")
    monkeypatch.setenv("STOCKPILE_ADMIN_PASSWORD", "admin-password-123")
    monkeypatch.setenv("STOCKPILE_ADMIN_USERNAME", "first-admin")
    monkeypatch.delenv("STOCKPILE_OPERATOR_PASSWORD", raising=False)
    init_db()

    monkeypatch.setenv("STOCKPILE_ADMIN_USERNAME", "renamed-admin")
    init_db()

    with sqlite3.connect(database_path) as connection:
        users = connection.execute(
            "SELECT id, username FROM users ORDER BY id"
        ).fetchall()
    assert users == [("rename-tenant-admin", "renamed-admin")]


def test_sheets_row_serializes_postgres_timestamps():
    timestamp = datetime(2026, 8, 23, 12, 30, tzinfo=timezone.utc)
    assert _sheet_value(timestamp) == "2026-08-23T12:30:00+00:00"


def test_sheets_row_contains_location_and_position_balances():
    transaction = {
        "id": "transaction-1",
        "created_at": "2026-08-23T12:30:00+00:00",
        "sku": "SKU-1",
        "barcode": "BARCODE-1",
        "product_name": "Textile roll",
        "location_code": "WH-MNL-A01",
        "location_name": "Rack A-01",
        "location_path": "Main Stockroom / Textile Floor / Rack A-01",
        "movement_type": "stock_out",
        "quantity": 3,
        "quantity_delta": -3,
        "location_balance_before": 10,
        "location_balance_after": 7,
        "balance_before": 25,
        "balance_after": 22,
        "actor_name": "Warehouse Operator",
        "reason": "Customer order",
        "source": "scanner",
        "reverses_transaction_id": None,
    }

    row = _transaction_sheet_row(transaction)
    values = dict(zip(SHEET_HEADERS, row, strict=True))
    assert values["location_code"] == "WH-MNL-A01"
    assert values["location_balance_before"] == 10
    assert values["location_balance_after"] == 7
    assert values["total_balance_after"] == 22


def test_failed_sheets_exchange_is_visible_and_admin_can_retry(client, monkeypatch):
    headers = login(client)
    admin_headers = login(client, "admin")

    def fail_exchange(_):
        raise RuntimeError("simulated Sheets outage")

    monkeypatch.setattr("services.sheets.append_transaction_to_sheet", fail_exchange)
    created = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(),
        headers=headers,
    )
    assert created.status_code == 200
    assert created.json()["exchange"]["status"] == "failed"
    transaction_id = created.json()["data"]["id"]

    monkeypatch.setattr("services.sheets.append_transaction_to_sheet", lambda _: None)
    forbidden = client.post(
        "/api/v1/inventory/exchange/retry",
        json={"transaction_id": transaction_id},
        headers=headers,
    )
    retried = client.post(
        "/api/v1/inventory/exchange/retry",
        json={"transaction_id": transaction_id},
        headers=admin_headers,
    )

    assert forbidden.status_code == 403
    assert retried.status_code == 200
    matching = next(
        row for row in retried.json()["data"] if row["transaction_id"] == transaction_id
    )
    assert matching["status"] == "synced"
    assert matching["attempts"] == 2


def test_sheets_provider_call_does_not_hold_database_write_lock(client, monkeypatch):
    headers = login(client)
    provider_started = Event()
    release_provider = Event()

    def wait_in_provider(_):
        provider_started.set()
        assert release_provider.wait(timeout=3)

    monkeypatch.setattr(
        "services.sheets.append_transaction_to_sheet", wait_in_provider
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.post,
            "/api/v1/inventory/transactions",
            json=transaction_payload(),
            headers=headers,
        )
        assert provider_started.wait(timeout=3)
        try:
            connection = sqlite3.connect(client.database_path, timeout=0.1)
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
            connection.close()
        finally:
            release_provider.set()

        response = future.result(timeout=3)

    assert response.status_code == 200
    assert response.json()["exchange"]["status"] == "synced"


def test_concurrent_sheets_retries_use_one_active_claim(client, monkeypatch):
    operator_headers = login(client)
    admin_headers = login(client, "admin")
    created = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(),
        headers=operator_headers,
    )
    transaction_id = created.json()["data"]["id"]
    assert created.json()["exchange"]["status"] == "not_configured"

    provider_started = Event()
    release_provider = Event()
    provider_calls = []

    def wait_in_provider(_):
        provider_calls.append(1)
        provider_started.set()
        assert release_provider.wait(timeout=3)

    monkeypatch.setattr(
        "services.sheets.append_transaction_to_sheet", wait_in_provider
    )
    retry_path = "/api/v1/inventory/exchange/retry"
    retry_body = {"transaction_id": transaction_id}

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            client.post, retry_path, json=retry_body, headers=admin_headers
        )
        assert provider_started.wait(timeout=3)
        second = executor.submit(
            client.post, retry_path, json=retry_body, headers=admin_headers
        )
        second_response = second.result(timeout=3)
        release_provider.set()
        first_response = first.result(timeout=3)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(provider_calls) == 1

    exchange = client.get(
        "/api/v1/inventory/exchange", headers=operator_headers
    ).json()
    matching = next(
        row for row in exchange["data"] if row["transaction_id"] == transaction_id
    )
    assert matching["status"] == "synced"
    assert matching["attempts"] == 1


def test_stale_sheets_claim_can_be_recovered(client, monkeypatch):
    operator_headers = login(client)
    admin_headers = login(client, "admin")
    created = client.post(
        "/api/v1/inventory/transactions",
        json=transaction_payload(),
        headers=operator_headers,
    ).json()
    transaction_id = created["data"]["id"]

    connection = sqlite3.connect(client.database_path)
    connection.execute(
        """
        UPDATE sheets_outbox
        SET claim_token = 'abandoned-worker', claimed_at = '2000-01-01T00:00:00+00:00'
        WHERE transaction_id = ?
        """,
        (transaction_id,),
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        "services.sheets.append_transaction_to_sheet", lambda _: None
    )
    retried = client.post(
        "/api/v1/inventory/exchange/retry",
        json={"transaction_id": transaction_id},
        headers=admin_headers,
    )

    assert retried.status_code == 200
    matching = next(
        row
        for row in retried.json()["data"]
        if row["transaction_id"] == transaction_id
    )
    assert matching["status"] == "synced"
    with sqlite3.connect(client.database_path) as connection:
        claim = connection.execute(
            "SELECT claim_token, claimed_at FROM sheets_outbox WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
    assert claim == (None, None)
