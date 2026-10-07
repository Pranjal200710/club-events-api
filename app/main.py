"""HTTP routes, validation, role checks, and the registration business rules."""

import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from .auth import hash_password, issue_tokens, token_hash, verify_password
from .db import DEFAULT_DB, connect, init_db

bearer = HTTPBearer(auto_error=False)


class Credentials(BaseModel):
    # Reject extra fields, including a malicious signup field "role": "admin".
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=30, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value):
        return value.lower()


class RefreshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=1, max_length=200)


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=100)
    capacity: int = Field(strict=True, ge=1, le=10000)
    starts_at: AwareDatetime

    @field_validator("starts_at")
    @classmethod
    def future_start(cls, value):
        if value <= datetime.now(timezone.utc):
            raise ValueError("Event must start in the future")
        return value.astimezone(timezone.utc)


def unauthorized():
    return HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})


def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    if credentials is None:
        raise unauthorized()
    with connect(request.app.state.db_path) as db:
        user = db.execute(
            """SELECT u.id, u.username, u.role, s.id AS session_id
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.access_hash = ? AND s.access_expires_at > ?""",
            (token_hash(credentials.credentials), int(time.time())),
        ).fetchone()
    if user is None:
        raise unauthorized()
    return dict(user)


User = Annotated[dict, Depends(current_user)]


def admin_only(user: User):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    return user


Admin = Annotated[dict, Depends(admin_only)]


def get_event(db, event_id):
    event = db.execute(
        """SELECT e.*, COUNT(r.user_id) AS registered_count,
                  e.capacity - COUNT(r.user_id) AS seats_left
           FROM events e LEFT JOIN registrations r ON e.id = r.event_id
           WHERE e.id = ? GROUP BY e.id""", (event_id,),
    ).fetchone()
    if event is None:
        raise HTTPException(404, "Event not found")
    return dict(event)


def create_app(db_path=DEFAULT_DB):
    @asynccontextmanager
    async def lifespan(app):
        init_db(app.state.db_path)
        yield

    app = FastAPI(
        title="Club Events API", version="1.0.0", lifespan=lifespan,
        description=(
            "Admins create events. Members (and admins) can reserve seats. "
            "Login returns a 15-minute access token and a rotating refresh token. "
            "Paste the access token into Authorize to try protected routes."
        ),
    )
    app.state.db_path = db_path

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, error):
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            return JSONResponse(
                status_code=503, content={"detail": "Database busy; retry shortly"},
                headers={"Retry-After": "1"},
            )
        raise error

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/docs")

    @app.post("/auth/signup", status_code=201, tags=["Authentication"])
    def signup(body: Credentials):
        # Hash before taking the database lock: hashing is intentionally slow.
        password_hash = hash_password(body.password)
        try:
            with connect(db_path, write=True) as db:
                cursor = db.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'member')",
                    (body.username, password_hash),
                )
                return {"id": cursor.lastrowid, "username": body.username, "role": "member"}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Username already exists") from None

    @app.post("/auth/login", tags=["Authentication"])
    def login(body: Credentials, response: Response):
        with connect(db_path) as db:
            user = db.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
        if user is None or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(401, "Incorrect username or password")
        with connect(db_path, write=True) as db:
            result = issue_tokens(db, user["id"])
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.post("/auth/refresh", tags=["Authentication"])
    def refresh(body: RefreshInput, response: Response):
        # Reading, deleting, and replacing the token happen under ONE DB lock.
        with connect(db_path, write=True) as db:
            session = db.execute(
                "SELECT * FROM sessions WHERE refresh_hash = ? AND refresh_expires_at > ?",
                (token_hash(body.refresh_token), int(time.time())),
            ).fetchone()
            if session is None:
                raise unauthorized()
            db.execute("DELETE FROM sessions WHERE id = ?", (session["id"],))
            result = issue_tokens(
                db, session["user_id"], refresh_expires_at=session["refresh_expires_at"],
            )
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.post("/auth/logout", status_code=204, tags=["Authentication"])
    def logout(body: RefreshInput):
        # The refresh token identifies the session even after its access expires.
        with connect(db_path, write=True) as db:
            db.execute("DELETE FROM sessions WHERE refresh_hash = ?", (token_hash(body.refresh_token),))
        return Response(status_code=204)

    @app.get("/users/me", tags=["Users"])
    def me(user: User):
        return {key: user[key] for key in ("id", "username", "role")}

    @app.post("/events", status_code=201, tags=["Events"])
    def create_event(body: EventInput, admin: Admin):
        with connect(db_path, write=True) as db:
            cursor = db.execute(
                "INSERT INTO events (title, capacity, starts_at, created_by) VALUES (?, ?, ?, ?)",
                (body.title, body.capacity, body.starts_at.isoformat(), admin["id"]),
            )
            return get_event(db, cursor.lastrowid)

    @app.get("/events", tags=["Events"])
    def list_events(
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=100),
        search: str = Query("", max_length=100),
        sort: Literal["date_asc", "date_desc", "title_asc"] = "date_asc",
    ):
        # Only these hard-coded SQL fragments can enter ORDER BY.
        order = {"date_asc": "e.starts_at ASC", "date_desc": "e.starts_at DESC",
                 "title_asc": "e.title COLLATE NOCASE ASC"}[sort]
        with connect(db_path) as db:
            # Keep total and items consistent if another request creates an event.
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*) FROM events WHERE title LIKE ?", (f"%{search}%",)).fetchone()[0]
            rows = db.execute(
                f"""SELECT e.*, COUNT(r.user_id) AS registered_count,
                           e.capacity - COUNT(r.user_id) AS seats_left
                    FROM events e LEFT JOIN registrations r ON e.id = r.event_id
                    WHERE e.title LIKE ? GROUP BY e.id
                    ORDER BY {order}, e.id ASC LIMIT ? OFFSET ?""",
                (f"%{search}%", page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total,
                "page": page, "page_size": page_size}

    @app.get("/events/{event_id}", tags=["Events"])
    def event_detail(event_id: int):
        with connect(db_path) as db:
            return get_event(db, event_id)

    @app.post("/events/{event_id}/registrations", status_code=201, tags=["Registrations"])
    def register(event_id: int, user: User):
        # Most important part of the project: lock -> check -> insert -> commit.
        with connect(db_path, write=True) as db:
            event = get_event(db, event_id)
            if datetime.fromisoformat(event["starts_at"]) <= datetime.now(timezone.utc):
                raise HTTPException(409, "Event has already started")
            duplicate = db.execute(
                "SELECT 1 FROM registrations WHERE user_id = ? AND event_id = ?",
                (user["id"], event_id),
            ).fetchone()
            if duplicate:
                raise HTTPException(409, "Already registered")
            if event["seats_left"] <= 0:
                raise HTTPException(409, "Event is full")
            db.execute(
                "INSERT INTO registrations (user_id, event_id) VALUES (?, ?)",
                (user["id"], event_id),
            )
            return {"user_id": user["id"], "event_id": event_id, "message": "Seat reserved"}

    @app.delete("/events/{event_id}/registrations/me", status_code=204, tags=["Registrations"])
    def cancel_registration(event_id: int, user: User):
        with connect(db_path, write=True) as db:
            get_event(db, event_id)
            deleted = db.execute(
                "DELETE FROM registrations WHERE user_id = ? AND event_id = ?",
                (user["id"], event_id),
            ).rowcount
            if not deleted:
                raise HTTPException(404, "You are not registered for this event")
        return Response(status_code=204)

    @app.get("/users/me/registrations", tags=["Registrations"])
    def my_registrations(user: User):
        with connect(db_path) as db:
            rows = db.execute(
                """SELECT e.id AS event_id, e.title, e.starts_at, r.registered_at
                   FROM registrations r JOIN events e ON e.id = r.event_id
                   WHERE r.user_id = ? ORDER BY e.starts_at, e.id""", (user["id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/events/{event_id}/registrations", tags=["Registrations"])
    def event_registrations(event_id: int, admin: Admin):
        with connect(db_path) as db:
            get_event(db, event_id)
            rows = db.execute(
                """SELECT u.id AS user_id, u.username, r.registered_at
                   FROM registrations r JOIN users u ON u.id = r.user_id
                   WHERE r.event_id = ? ORDER BY r.registered_at, u.id""", (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    return app


app = create_app()
