import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware

from config import frontend_url, validate_runtime_config
from database import init_db
from routers import auth, inventory


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("stockpile.api")

# Route-specific checks enforce exact file limits. This transport cap leaves
# enough room for a 10 MB product photo plus multipart framing.
MAX_REQUEST_BYTES = 10 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_config()
    init_db()
    yield


app = FastAPI(
    title="Stockpile API",
    version="2.0.0",
    description=(
        "Authoritative inventory transactions, barcode lookup, immutable history, "
        "and one-way Google Sheets exchange."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_url()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Stockpile-Filename",
    ],
)


@app.middleware("http")
async def request_safety_and_logging(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return Response(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content="Request payload is too large.",
                )
        except ValueError:
            return Response(
                status_code=status.HTTP_400_BAD_REQUEST,
                content="Invalid Content-Length header.",
            )

    started_at = time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        logger.info(
            json.dumps(
                {
                    "event": "api_request",
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                }
            )
        )
    return response


@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "authority": "Stockpile application database"}


app.include_router(auth.router)
app.include_router(inventory.router)
