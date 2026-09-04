import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger("stockpile.config")

_EPHEMERAL_AUTH_SECRET = secrets.token_urlsafe(48)
_AUTH_WARNING_EMITTED = False


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def database_url() -> str | None:
    value = os.getenv("DATABASE_URL", "").strip()
    return value or None


def database_file() -> Path:
    configured = os.getenv("STOCKPILE_DB_PATH", "").strip()
    return Path(configured).expanduser().resolve() if configured else BASE_DIR / "inventory.db"


def frontend_url() -> str:
    return os.getenv("FRONTEND_URL", "http://localhost:3000").strip()


def environment() -> str:
    return os.getenv("STOCKPILE_ENV", "development").strip().lower()


def auth_secret() -> str:
    global _AUTH_WARNING_EMITTED
    configured = os.getenv("STOCKPILE_AUTH_SECRET", "").strip()
    if configured:
        if len(configured) < 32:
            raise RuntimeError("STOCKPILE_AUTH_SECRET must contain at least 32 characters.")
        return configured

    if environment() == "production":
        raise RuntimeError(
            "STOCKPILE_AUTH_SECRET must be configured when STOCKPILE_ENV=production."
        )

    if not _AUTH_WARNING_EMITTED:
        logger.warning(
            "STOCKPILE_AUTH_SECRET is not configured. Using an ephemeral local-only signing secret; "
            "sessions will be invalidated on restart."
        )
        _AUTH_WARNING_EMITTED = True
    return _EPHEMERAL_AUTH_SECRET


def validate_runtime_config() -> None:
    signing_secret = auth_secret()
    if environment() != "production":
        return
    if signing_secret == "replace-with-a-long-random-secret":
        raise RuntimeError(
            "Replace the example STOCKPILE_AUTH_SECRET before production startup."
        )
    if seed_demo_data():
        raise RuntimeError("STOCKPILE_SEED_DEMO_DATA must be false in production.")
    placeholder_passwords = {
        "replace-with-admin-password",
        "replace-with-operator-password",
    }
    configured_passwords = {
        os.getenv("STOCKPILE_ADMIN_PASSWORD", "").strip(),
        os.getenv("STOCKPILE_OPERATOR_PASSWORD", "").strip(),
    }
    if placeholder_passwords & configured_passwords:
        raise RuntimeError(
            "Replace example Stockpile account passwords before production startup."
        )


def token_ttl_seconds() -> int:
    try:
        return max(300, int(os.getenv("STOCKPILE_TOKEN_TTL_SECONDS", "28800")))
    except ValueError:
        return 28800


def tenant_id() -> str:
    return os.getenv("STOCKPILE_TENANT_ID", "stockpile-demo").strip() or "stockpile-demo"


def seed_demo_data() -> bool:
    return env_bool("STOCKPILE_SEED_DEMO_DATA", False)


def sheets_spreadsheet_id() -> str:
    return os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()


def sheets_range() -> str:
    return os.getenv("GOOGLE_SHEETS_RANGE", "Transactions!A:T").strip()


def google_service_account_file() -> str:
    return os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()


def google_service_account_json() -> str:
    return os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
