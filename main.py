import base64
import json
import math
import time
import uuid
from collections import defaultdict, deque
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Orders API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

TOTAL_ORDERS = 42
RATE_LIMIT = 19
WINDOW_SECONDS = 10

# In-memory state: suitable for a single Render instance.
idempotency_store = {}
rate_buckets = defaultdict(deque)
state_lock = Lock()


class OrderRequest(BaseModel):
    product: str = "default"
    quantity: int = 1


def check_rate_limit(request: Request):
    client_id = request.headers.get("X-Client-Id")
    if not client_id:
        client_id = request.client.host if request.client else "unknown"

    now = time.monotonic()

    with state_lock:
        bucket = rate_buckets[client_id]

        while bucket and now - bucket[0] >= WINDOW_SECONDS:
            bucket.popleft()

        if len(bucket) >= RATE_LIMIT:
            retry_after = max(
                1, math.ceil(WINDOW_SECONDS - (now - bucket[0]))
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)

    return None


@app.get("/")
def health():
    return {"status": "ok", "service": "orders-api"}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/orders", status_code=201)
def create_order(
    request: Request,
    body: OrderRequest,
    idempotency_key: str = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    limited = check_rate_limit(request)
    if limited:
        return limited

    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required",
        )

    with state_lock:
        if idempotency_key in idempotency_store:
            return idempotency_store[idempotency_key]

        order = {
            "id": str(uuid.uuid4()),
            "product": body.product,
            "quantity": body.quantity,
        }
        idempotency_store[idempotency_key] = order
        return order


@app.get("/orders")
def list_orders(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    cursor: str = Query(default=None),
):
    limited = check_rate_limit(request)
    if limited:
        return limited

    # Cursor represents the next zero-based catalog offset.
    offset = 0
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(
                cursor.encode("ascii")
            ).decode("ascii")
            offset = int(decoded)
            if offset < 0 or offset > TOTAL_ORDERS:
                raise ValueError("Invalid offset")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    end = min(offset + limit, TOTAL_ORDERS)
    items = [
        {"id": order_id}
        for order_id in range(offset + 1, end + 1)
    ]

    next_cursor = None
    if end < TOTAL_ORDERS:
        next_cursor = base64.urlsafe_b64encode(
            str(end).encode("ascii")
        ).decode("ascii")

    return {
        "items": items,
        "next_cursor": next_cursor,
    }
