# Club Events API

A small Python backend for a college club: administrators create events, and members reserve or cancel seats. Database transactions prevent an event from being overbooked when requests arrive together.

Built for **WEB-BE1: Multi-Resource API with Roles & Concurrency Safety**. The interface for testing the API is FastAPI's interactive documentation at `/docs`.

## Run it

Use Python 3.12. No separate database server, API key, or cloud account is needed.

From this project's folder in the VS Code terminal (Windows PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.create_admin
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

The admin command asks for a username and password. Password typing is hidden. Keep the credentials yourself; there is no built-in administrator password. If `py` is unavailable, use your installed Python executable instead.

Open **http://127.0.0.1:8000/docs**. Keep the server terminal open; press `Ctrl+C` to stop it. On this prepared Windows checkout, `.venv` is already set up: `CREATE-ADMIN.cmd`, `RUN.cmd`, and `TEST.cmd` are shortcuts for the same commands. A fresh GitHub clone needs the setup commands above first.

macOS/Linux equivalents:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m app.create_admin
.venv/bin/python -m uvicorn app.main:app --reload
```

SQLite creates `data/club.db` on first startup. The database, local environment, and credentials are excluded from Git. The admin account must be created on each new installation.

## Try the whole flow in the browser

1. Open `POST /auth/login`, click **Try it out**, and enter the admin credentials you created. Click **Execute**.
2. Copy the returned `access_token`. Click **Authorize** at the top of the page, paste only the token (without quotes or the word `Bearer`), and confirm.
3. Call `POST /events` with the example below. Use a future date and keep the returned event `id`.
4. Call `POST /auth/signup` with a member username and password. Public signup always creates a member.
5. Log in as the member. Replace the token under **Authorize** (log out of the authorization dialog first if necessary).
6. Try `POST /events` again: it returns **403** because a member is not an admin.
7. Call `POST /events/{event_id}/registrations` using your event ID: it returns **201**. Try again: **409**, already registered.
8. Create and log in as a second member. Register for the same capacity-one event: **409**, event full.
9. Log back in as the first member and call `DELETE /events/{event_id}/registrations/me`. The second member can now register.
10. Use `POST /auth/refresh` with your latest refresh token. It returns a new pair; the previous pair no longer works. Replace the access token in **Authorize**. `POST /auth/logout` accepts the latest refresh token and invalidates that session.

Example event body (change the date if necessary):

```json
{
  "title": "Python workshop",
  "capacity": 1,
  "starts_at": "2099-01-15T10:00:00+05:30"
}
```

Example member signup body (demo values, not real account credentials):

```json
{
  "username": "alice",
  "password": "ChooseYourOwnPassword123!"
}
```

Refresh/logout body:

```json
{
  "refresh_token": "paste_your_latest_refresh_token_here"
}
```

## Requirements covered

| Assignment requirement | Implementation |
| --- | --- |
| Related resources and real relationships | `users` and `events`, linked by the `registrations` join table; enforced foreign keys |
| At least two roles | `admin` and `member`; only admins create events and read event attendee lists |
| Short-lived access plus refresh flow | Access lasts at most 15 minutes; refresh lasts seven days, rotates on use, and does not extend the original session expiry |
| Safe concurrent operation | `BEGIN IMMEDIATE` obtains SQLite's write lock before checking capacity and inserting a registration |
| Pagination/filtering/sorting | `GET /events?page=1&page_size=10&search=Python&sort=date_asc` |
| 3–5 meaningful automated tests | Five integration tests, including real concurrent HTTP requests against a temporary SQLite database |

## Endpoints

| Method and path | Access | Purpose |
| --- | --- | --- |
| `POST /auth/signup` | Public | Create a member |
| `POST /auth/login` | Public, valid credentials required | Obtain access and refresh tokens |
| `POST /auth/refresh` | Valid refresh token required | Replace the current token pair |
| `POST /auth/logout` | Refresh token identifies the session | Revoke that token pair; repeated logout is harmless |
| `GET /users/me` | Logged-in user | Show your ID, username and role |
| `POST /events` | Admin | Create an event |
| `GET /events` | Public | Browse, search, sort and paginate events |
| `GET /events/{event_id}` | Public | Show event details and remaining seats |
| `POST /events/{event_id}/registrations` | Logged-in user | Reserve your own seat |
| `DELETE /events/{event_id}/registrations/me` | Logged-in user | Cancel your own seat |
| `GET /users/me/registrations` | Logged-in user | List your registrations |
| `GET /events/{event_id}/registrations` | Admin | List an event's attendees |

Admins can also register for events. Members cannot create events or access attendee lists. A client never chooses the user ID for a registration; the server reads it from the authenticated session.

List sorting accepts `date_asc`, `date_desc`, or `title_asc`. Page size is 1–100. Invalid fields return 422; missing/expired authentication returns 401; insufficient permissions return 403; absent resources return 404; conflicts return 409. A write lock that stays busy for five seconds produces 503 with a retry hint.

## How the last-seat protection works

```text
Request A: BEGIN IMMEDIATE -> count seats -> insert -> COMMIT
Request B: waits for A's write lock ... -> count seats again -> full -> 409
```

The database lock is acquired **before** the seat count is read and held until the insertion commits. SQLite allows one writer at a time, including writers in different processes using the same local database file. There is no Python lock or unlocked check-then-insert sequence. An exception rolls back the transaction. A composite primary key independently prevents the same user from registering twice.

SQLite uses a database-wide write lock here, **not row-level locking**. That is sufficient for this assignment and a small local application. At larger scale, PostgreSQL could lock the specific event row to allow independent events to be updated simultaneously. See the [SQLite transaction documentation](https://sqlite.org/lang_transaction.html).

## Authentication choices

Tokens are **opaque random strings**, not JWTs. The brief requires expiring access tokens and a refresh flow; it does not require a particular token format. Each protected request looks up the SHA-256 hash of the supplied access token and checks its expiry. Roles come from the user table.

The database stores only hashes of both tokens. Passwords use salted `hashlib.scrypt`, a deliberately expensive password-hashing function. Refreshing atomically deletes the old session row and inserts its replacement, so simultaneous refresh attempts cannot both consume the same token. Logout deletes the session. The simplicity trade-off is one database lookup per authenticated request.

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The five tests cover:

1. Member/admin restrictions, blocked role injection, password hashing, and input validation.
2. Access/refresh expiry, rotation, logout, hashed token storage, and simultaneous refresh attempts.
3. Relationships, duplicate registrations, cancellation ownership, foreign keys, rollback, and closed events.
4. Eight users competing concurrently for one seat: exactly **one 201**, **seven 409s**, and **one stored registration**.
5. Search, pagination, sorting, invalid query values, and SQL-injection-like input.

Tests use temporary database files and do not change your local accounts or events. Verified on Python 3.12 with the pinned dependencies. The pinned Starlette release emits a deprecation warning for its HTTPX-based test client; this does not fail the tests.

## Read the code in this order

1. `app/schema.sql`: tables, keys and relationships.
2. `app/db.py`: connections, transactions, commit and rollback.
3. `app/auth.py`: password hashing, random tokens and expiry.
4. `app/main.py`: input models, role checks and API routes.
5. `app/create_admin.py`: trusted local admin creation.
6. `tests/test_api.py`: examples that prove the rules.

See **[INTERVIEW_GUIDE.md](INTERVIEW_GUIDE.md)** for a beginner walkthrough, likely questions, and a short practice plan.

## Scope and trade-offs

This is an educational local API. The browser interface is Swagger UI, which FastAPI generates from the routes. Its default JavaScript/CSS assets load from a CDN, so the documentation page needs internet access; the API itself uses the local database.

Possible next steps for a public service: HTTPS, login rate limiting, uniform login timing for unknown usernames, expired-session cleanup, database migrations, and a browser client that keeps refresh tokens in secure HttpOnly cookies. Those are separate from the required project. There is no password-reset flow or event-edit endpoint. Tests exercise concurrent clients in one process; SQLite's documented file locking supplies coordination across processes too.

## Submission note

Suggested reviewer note:

> Club event registration API built with Python, FastAPI, and SQLite. Includes admin/member roles, 15-minute access tokens with rotating refresh tokens, a foreign-key-backed registration join table, transaction-protected capacity limits, pagination/search/sort, and five integration tests. Setup, an interactive API demo, and design trade-offs are documented in the README.

Upload the project source to a public GitHub repository, and submit its repository URL for **WEB-BE1 (Web Development)**. Do not submit it under AIML-04. `.venv`, `data/`, and personal credentials must stay untracked.
