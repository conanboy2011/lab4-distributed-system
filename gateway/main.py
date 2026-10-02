import os
import asyncio

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx


app = FastAPI(title="API Gateway - Library System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


RATING_SERVICE_URL = os.getenv(
    "RATING_SERVICE_URL",
    "http://user-service:8050"
)

LIBRARY_SERVICE_URL = os.getenv(
    "LIBRARY_SERVICE_URL",
    "http://library-service:8060"
)

RESERVATION_SERVICE_URL = os.getenv(
    "RESERVATION_SERVICE_URL",
    "http://reservation-service:8070"
)


@app.get("/manage/health")
def health_check():
    return {"status": "OK"}


async def forward_request(
    service_url: str,
    path: str,
    request: Request,
    retries: int = 0
):
    url = f"{service_url.rstrip('/')}/{path.lstrip('/')}"

    body = await request.body()

    headers = dict(request.headers)
    headers.pop("host", None)

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(
                timeout=5.0
            ) as client:
                response = await client.request(
                    method=request.method,
                    url=url,
                    content=body,
                    params=request.query_params,
                    headers=headers
                )

            response_headers = dict(response.headers)
            response_headers.pop("content-length", None)

            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=response_headers
            )

        except httpx.RequestError:
            if attempt < retries:
                await asyncio.sleep(1)
                continue

            return Response(
                content='{"message":"Bonus Service unavailable"}',
                status_code=503,
                media_type="application/json"
            )


# =========================
# LIBRARY
# =========================

@app.get("/api/v1/libraries")
async def get_libraries(request: Request):
    return await forward_request(
        LIBRARY_SERVICE_URL,
        "/libraries",
        request
    )


@app.get("/api/v1/libraries/{library_uid}/books")
async def get_library_books(
    library_uid: str,
    request: Request
):
    return await forward_request(
        LIBRARY_SERVICE_URL,
        f"/libraries/{library_uid}/books",
        request
    )


# =========================
# RATING
# =========================

@app.get("/api/v1/rating")
async def get_rating(request: Request):
    return await forward_request(
        RATING_SERVICE_URL,
        "/rating",
        request,
        retries=5
    )


# =========================
# RESERVATION
# =========================

@app.get("/api/v1/reservations")
async def get_reservations(request: Request):
    return await forward_request(
        RESERVATION_SERVICE_URL,
        "/reservations",
        request
    )


@app.post("/api/v1/reservations")
async def create_reservation(request: Request):
    return await forward_request(
        RESERVATION_SERVICE_URL,
        "/reservations",
        request
    )


@app.post("/api/v1/reservations/{reservation_uid}/return")
async def return_reservation(
    reservation_uid: str,
    request: Request
):
    return await forward_request(
        RESERVATION_SERVICE_URL,
        f"/reservations/{reservation_uid}/return",
        request
    )

