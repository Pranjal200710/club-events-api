"""Five integration tests against real SQLite files and real HTTP handlers."""

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth import ACCESS_SECONDS, hash_password, token_hash, verify_password
from app.db import connect
from app.main import create_app

PASSWORD = "TestPassword123!"  # Test-only accounts in temporary databases.


def future_date(days=7):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def headers(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def signup_and_login(client, username):
    body = {"username": username, "password": PASSWORD}
    assert client.post("/auth/signup", json=body).status_code == 201
    login = client.post("/auth/login", json=body)
    assert login.status_code == 200
    return login.json()


@pytest.fixture
def api(tmp_path):
    path = tmp_path / "test.db"
    app = create_app(path)
    with TestClient(app) as client:
        # Only the trusted test setup can seed an admin directly.
        with connect(path, write=True) as db:
            db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                ("admin", hash_password(PASSWORD)),
            )
        tokens = client.post("/auth/login", json={"username": "admin", "password": PASSWORD}).json()
        yield app, client, path, headers(tokens)


def make_event(client, admin, capacity=2, title="Python workshop", days=7):
    result = client.post("/events", headers=admin, json={
        "title": title, "capacity": capacity, "starts_at": future_date(days),
    })
    assert result.status_code == 201, result.text
    return result.json()["id"]


def test_roles_passwords_and_input_validation(api):
    _, client, path, admin = api
    member = headers(signup_and_login(client, "Alice"))
    assert client.get("/users/me", headers=member).json()["role"] == "member"
    assert client.get("/users/me").status_code == 401
    assert client.post("/auth/login", json={"username": "alice", "password": "wrongpassword"}).status_code == 401
    assert client.post("/auth/signup", json={"username": "ALICE", "password": PASSWORD}).status_code == 409
    assert client.post("/auth/signup", json={
        "username": "intruder", "password": PASSWORD, "role": "admin",
    }).status_code == 422
    body = {"title": "Workshop", "capacity": 2, "starts_at": future_date()}
    assert client.post("/events", json=body).status_code == 401
    assert client.post("/events", headers=member, json=body).status_code == 403
    event_id = make_event(client, admin)
    assert client.get(f"/events/{event_id}/registrations", headers=member).status_code == 403
    for invalid in ({"capacity": 0}, {"capacity": True}, {"title": "   "},
                    {"starts_at": future_date(-1)}, {"starts_at": "2099-01-01T10:00:00"}):
        assert client.post("/events", headers=admin, json=body | invalid).status_code == 422
    with connect(path) as db:
        stored = db.execute("SELECT password_hash FROM users WHERE username = 'alice'").fetchone()[0]
    assert stored != PASSWORD and verify_password(PASSWORD, stored)
    assert hash_password(PASSWORD) != stored  # Independent random salts.


def test_token_expiry_rotation_logout_and_concurrent_refresh(api):
    app, client, path, _ = api
    tokens = signup_and_login(client, "alice")
    assert 0 < tokens["expires_in"] <= ACCESS_SECONDS
    assert client.get("/users/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}).status_code == 401
    with connect(path, write=True) as db:
        session = db.execute("SELECT * FROM sessions WHERE access_hash = ?", (token_hash(tokens["access_token"]),)).fetchone()
        assert session["access_hash"] != tokens["access_token"]
        assert session["refresh_hash"] != tokens["refresh_token"]
        original_expiry = session["refresh_expires_at"]
        db.execute("UPDATE sessions SET access_expires_at = ? WHERE id = ?", (int(time.time()) - 1, session["id"]))
    assert client.get("/users/me", headers=headers(tokens)).status_code == 401

    # Two separate clients attempt to consume the SAME refresh token together.
    barrier = threading.Barrier(2)

    def refresh_once(_):
        with TestClient(app) as separate_client:
            barrier.wait(timeout=10)
            return separate_client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(refresh_once, range(2)))
    assert sorted(r.status_code for r in responses) == [200, 401]
    rotated = next(r.json() for r in responses if r.status_code == 200)
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert client.get("/users/me", headers=headers(rotated)).status_code == 200
    assert client.get("/users/me", headers=headers(tokens)).status_code == 401
    with connect(path) as db:
        expiry = db.execute("SELECT refresh_expires_at FROM sessions WHERE refresh_hash = ?", (token_hash(rotated["refresh_token"]),)).fetchone()[0]
    assert expiry == original_expiry
    assert client.post("/auth/logout", json={"refresh_token": rotated["refresh_token"]}).status_code == 204
    assert client.get("/users/me", headers=headers(rotated)).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]}).status_code == 401

    expired = client.post("/auth/login", json={"username": "alice", "password": PASSWORD}).json()
    with connect(path, write=True) as db:
        db.execute("UPDATE sessions SET refresh_expires_at = ? WHERE refresh_hash = ?",
                   (int(time.time()) - 1, token_hash(expired["refresh_token"])))
    assert client.post("/auth/refresh", json={"refresh_token": expired["refresh_token"]}).status_code == 401


def test_relationship_duplicate_cancellation_and_rollback(api):
    _, client, path, admin = api
    alice = headers(signup_and_login(client, "alice"))
    bob = headers(signup_and_login(client, "bob"))
    event_id = make_event(client, admin, capacity=1)
    route = f"/events/{event_id}/registrations"
    assert client.post(route, headers=alice).status_code == 201
    duplicate = client.post(route, headers=alice)
    assert duplicate.status_code == 409 and duplicate.json()["detail"] == "Already registered"
    assert client.get("/users/me/registrations", headers=alice).json()[0]["event_id"] == event_id
    assert client.get(route, headers=admin).json()[0]["username"] == "alice"
    assert client.delete(route + "/me", headers=bob).status_code == 404
    assert client.post(route, headers=bob).status_code == 409
    assert client.delete(route + "/me", headers=alice).status_code == 204
    assert client.get(f"/events/{event_id}").json()["seats_left"] == 1
    assert client.post(route, headers=bob).status_code == 201
    assert client.post("/events/9999/registrations", headers=alice).status_code == 404

    # A bad foreign key fails at the DB layer and rolls back the earlier insert.
    with pytest.raises(sqlite3.IntegrityError):
        with connect(path, write=True) as db:
            db.execute("INSERT INTO events (title, capacity, starts_at, created_by) VALUES (?, ?, ?, ?)",
                       ("Must roll back", 1, future_date(), 1))
            db.execute("INSERT INTO registrations (user_id, event_id) VALUES (?, ?)", (9999, event_id))
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE title = 'Must roll back'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM registrations WHERE event_id = ?", (event_id,)).fetchone()[0] == 1
    with connect(path, write=True) as db:
        db.execute("UPDATE events SET starts_at = ? WHERE id = ?", (future_date(-1), event_id))
    past = client.post(route, headers=alice)
    assert past.status_code == 409 and "already started" in past.json()["detail"]


def test_concurrent_requests_cannot_oversell_last_seat(api):
    app, client, path, admin = api
    event_id = make_event(client, admin, capacity=1)
    members = [headers(signup_and_login(client, f"member_{number}")) for number in range(8)]
    barrier = threading.Barrier(len(members))

    def reserve(member_headers):
        with TestClient(app) as separate_client:
            barrier.wait(timeout=10)
            return separate_client.post(f"/events/{event_id}/registrations", headers=member_headers)

    with ThreadPoolExecutor(max_workers=len(members)) as pool:
        results = list(pool.map(reserve, members))
    assert sorted(result.status_code for result in results) == [201] + [409] * 7
    assert all(result.json()["detail"] == "Event is full" for result in results if result.status_code == 409)
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM registrations WHERE event_id = ?", (event_id,)).fetchone()[0] == 1
    assert client.get(f"/events/{event_id}").json()["seats_left"] == 0


def test_pagination_search_and_sort(api):
    _, client, _, admin = api
    make_event(client, admin, title="Python basics", days=2)
    make_event(client, admin, title="Python advanced", days=3)
    make_event(client, admin, title="Cloud basics", days=1)
    query = {"search": "Python", "page_size": 1, "sort": "title_asc"}
    first = client.get("/events", params=query).json()
    second = client.get("/events", params=query | {"page": 2}).json()
    assert first["total"] == 2
    assert first["items"][0]["title"] == "Python advanced"
    assert second["items"][0]["title"] == "Python basics"
    assert client.get("/events", params=query | {"page": 3}).json()["items"] == []
    assert client.get("/events", params={"sort": "date_desc"}).json()["items"][0]["title"] == "Python advanced"
    assert client.get("/events", params={"page": 0}).status_code == 422
    assert client.get("/events", params={"page_size": 101}).status_code == 422
    assert client.get("/events", params={"sort": "title; DROP TABLE events"}).status_code == 422
    assert client.get("/events", params={"search": "' OR 1=1 --"}).json()["total"] == 0
