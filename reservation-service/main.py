from datetime import datetime
import asyncio
import os

import httpx
from fastapi import FastAPI, Depends, HTTPException, Header, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

import models
from database import engine, get_db


models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Reservation Service")


LIBRARY_SERVICE_URL = os.getenv(
    "LIBRARY_SERVICE_URL",
    "http://library-service:8060"
)

RATING_SERVICE_URL = os.getenv(
    "RATING_SERVICE_URL",
    "http://user-service:8050"
)


class BonusServiceUnavailable(Exception):
    pass


@app.exception_handler(BonusServiceUnavailable)
async def bonus_service_unavailable_handler(request, exc):
    return JSONResponse(
        status_code=503,
        content={"message": "Bonus Service unavailable"}
    )


def format_date(value):
    return value.strftime("%Y-%m-%d")


# ============================================================
# PENDING RATING
# ============================================================

def init_pending_rating_table():
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS pending_rating (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                delta INTEGER NOT NULL
            )
        """))


def save_pending_rating(username, delta):
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO pending_rating (username, delta)
                VALUES (:username, :delta)
            """),
            {
                "username": username,
                "delta": delta
            }
        )


def get_pending_ratings():
    with engine.begin() as connection:
        result = connection.execute(
            text("""
                SELECT id, username, delta
                FROM pending_rating
                ORDER BY id
            """)
        )

        return result.fetchall()


def delete_pending_rating(pending_id):
    with engine.begin() as connection:
        connection.execute(
            text("""
                DELETE FROM pending_rating
                WHERE id = :id
            """),
            {
                "id": pending_id
            }
        )


def try_apply_pending_ratings():
    pending = get_pending_ratings()

    for pending_id, username, delta in pending:
        try:
            response = httpx.post(
                f"{RATING_SERVICE_URL}/rating/change",
                json={
                    "username": username,
                    "delta": delta
                },
                timeout=2
            )

            if response.status_code == 200:
                delete_pending_rating(pending_id)

        except httpx.RequestError:
            # Rating Service is still unavailable.
            # Keep the pending operation in the database.
            pass


async def pending_rating_worker():
    while True:
        try:
            await asyncio.to_thread(
                try_apply_pending_ratings
            )
        except Exception:
            pass

        await asyncio.sleep(1)


@app.on_event("startup")
async def startup_event():
    init_pending_rating_table()

    asyncio.create_task(
        pending_rating_worker()
    )


# ============================================================
# LIBRARY SERVICE
# ============================================================

def get_library_book(library_uid, book_uid):
    url = (
        f"{LIBRARY_SERVICE_URL}"
        f"/internal/libraries/{library_uid}/books/{book_uid}"
    )

    try:
        response = httpx.get(
            url,
            timeout=10
        )
    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Library Service unavailable"
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail="Book or library not found"
        )

    return response.json()


def change_library_count(
    library_uid,
    book_uid,
    action,
    condition=None
):
    url = (
        f"{LIBRARY_SERVICE_URL}"
        f"/internal/libraries/{library_uid}/books/{book_uid}/{action}"
    )

    data = {}

    if condition is not None:
        data["condition"] = condition

    try:
        response = httpx.post(
            url,
            json=data,
            timeout=10
        )
    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Library Service unavailable"
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()


# ============================================================
# RATING SERVICE
# ============================================================

def get_rating(username):
    try:
        response = httpx.get(
            f"{RATING_SERVICE_URL}/rating",
            headers={
                "X-User-Name": username
            },
            timeout=2
        )
    except httpx.RequestError:
        raise BonusServiceUnavailable()

    if response.status_code != 200:
        raise BonusServiceUnavailable()

    return response.json()


def change_rating(username, delta):
    try:
        response = httpx.post(
            f"{RATING_SERVICE_URL}/rating/change",
            json={
                "username": username,
                "delta": delta
            },
            timeout=2
        )
    except httpx.RequestError:
        raise BonusServiceUnavailable()

    if response.status_code != 200:
        raise BonusServiceUnavailable()

    return response.json()


# ============================================================
# HEALTH
# ============================================================

@app.get("/manage/health")
def health_check():
    return {"status": "OK"}


# ============================================================
# RESERVATIONS
# ============================================================

@app.get("/reservations")
def get_reservations(
    x_user_name: str = Header(...),
    db: Session = Depends(get_db)
):
    reservations = (
        db.query(models.Reservation)
        .filter(
            models.Reservation.username == x_user_name,
            models.Reservation.status == "RENTED"
        )
        .all()
    )

    result = []

    for reservation in reservations:
        details = get_library_book(
            str(reservation.library_uid),
            str(reservation.book_uid)
        )

        result.append({
            "reservationUid": str(reservation.reservation_uid),
            "status": reservation.status,
            "startDate": format_date(reservation.start_date),
            "tillDate": format_date(reservation.till_date),
            "book": details["book"],
            "library": details["library"]
        })

    return result


@app.post("/reservations")
def create_reservation(
    data: dict,
    x_user_name: str = Header(...),
    db: Session = Depends(get_db)
):
    book_uid = data.get("bookUid")
    library_uid = data.get("libraryUid")
    till_date = data.get("tillDate")

    if not book_uid or not library_uid or not till_date:
        raise HTTPException(
            status_code=400,
            detail="Invalid request"
        )

    try:
        till_date_value = datetime.fromisoformat(till_date)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Invalid tillDate"
        )

    rented_count = (
        db.query(models.Reservation)
        .filter(
            models.Reservation.username == x_user_name,
            models.Reservation.status == "RENTED"
        )
        .count()
    )

    rating = get_rating(x_user_name)
    stars = rating["stars"]

    if rented_count >= stars:
        raise HTTPException(
            status_code=409,
            detail="Maximum number of rented books reached"
        )

    details = get_library_book(
        library_uid,
        book_uid
    )

    if details["availableCount"] <= 0:
        raise HTTPException(
            status_code=409,
            detail="Book is not available"
        )

    change_library_count(
        library_uid,
        book_uid,
        "borrow"
    )

    reservation = models.Reservation(
        username=x_user_name,
        book_uid=book_uid,
        library_uid=library_uid,
        status="RENTED",
        start_date=datetime.now(),
        till_date=till_date_value
    )

    db.add(reservation)
    db.commit()
    db.refresh(reservation)

    return {
        "reservationUid": str(reservation.reservation_uid),
        "status": reservation.status,
        "startDate": format_date(reservation.start_date),
        "tillDate": format_date(reservation.till_date),
        "book": details["book"],
        "library": details["library"],
        "rating": {
            "stars": stars
        }
    }


@app.post("/reservations/{reservation_uid}/return")
def return_book(
    reservation_uid: str,
    data: dict,
    x_user_name: str = Header(...),
    db: Session = Depends(get_db)
):
    reservation = (
        db.query(models.Reservation)
        .filter(
            models.Reservation.reservation_uid == reservation_uid,
            models.Reservation.username == x_user_name,
            models.Reservation.status == "RENTED"
        )
        .first()
    )

    if not reservation:
        raise HTTPException(
            status_code=404,
            detail="Reservation not found"
        )

    return_date = data.get("date")
    condition = data.get("condition")

    if not return_date or not condition:
        raise HTTPException(
            status_code=400,
            detail="Invalid request"
        )

    try:
        return_date_value = datetime.fromisoformat(return_date)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Invalid date"
        )

    if condition not in [
        "EXCELLENT",
        "GOOD",
        "BAD"
    ]:
        raise HTTPException(
            status_code=400,
            detail="Invalid condition"
        )

    details = get_library_book(
        str(reservation.library_uid),
        str(reservation.book_uid)
    )

    original_condition = details["book"]["condition"]

    is_late = return_date_value > reservation.till_date

    condition_worsened = (
        ["EXCELLENT", "GOOD", "BAD"].index(condition)
        >
        ["EXCELLENT", "GOOD", "BAD"].index(original_condition)
    )

    if is_late:
        reservation.status = "EXPIRED"
        rating_delta = -10
    else:
        reservation.status = "RETURNED"

        if condition_worsened:
            rating_delta = -10
        else:
            rating_delta = 1

    change_library_count(
        str(reservation.library_uid),
        str(reservation.book_uid),
        "return",
        condition
    )

    # Rating is an auxiliary service.
    #
    # If it is available, update it immediately.
    # If it is unavailable, save the operation in PostgreSQL.
    # The background worker will retry it after the service returns.
    try:
        change_rating(
            x_user_name,
            rating_delta
        )
    except BonusServiceUnavailable:
        save_pending_rating(
            x_user_name,
            rating_delta
        )

    db.commit()

    return Response(status_code=204)

