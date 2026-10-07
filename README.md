# Club Events API

I made this project for the **WEB-BE1: Multi-Resource API with Roles & Concurrency Safety** task.

The idea is a club event registration system. An admin can create an event with a fixed number of seats, and members can register or cancel their registration. The main problem I wanted to handle is what happens when two people try to book the last seat at the same time.

## Tech stack

I used Python, FastAPI and SQLite.

- **FastAPI** handles the API routes and gives me a page at `/docs` to try them.
- **SQLite** stores the data in a local file, so there is no separate database server to set up.
- **pytest** and **HTTPX** are used for the tests.

I kept this as a backend project. There is no separate frontend; the API can be tried through the docs page.

## Setup

I used Python 3.12. Run these commands from the project folder in the VS Code terminal.

### Windows

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.create_admin
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

If `py` is not available, use the installed Python executable in the first command.

### macOS / Linux

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m app.create_admin
.venv/bin/python -m uvicorn app.main:app --reload
```

The admin command asks for a username and password. The password is hidden while typing. I have not added a default admin account, so one needs to be created during setup.

Open **http://127.0.0.1:8000/docs** once the server starts. Press `Ctrl+C` in the terminal to stop it.

I also included three Windows shortcuts: `CREATE-ADMIN.cmd`, `RUN.cmd` and `TEST.cmd`. These work after setting up `.venv` and installing the requirements.

The database is created at `data/club.db` on first startup. I excluded the database, virtual environment and local credentials from Git.

## How the data is stored

I used four tables:

| Table | What it stores |
| --- | --- |
| `users` | Username, password hash and role |
| `events` | Title, capacity, start time and the admin who created it |
| `registrations` | The user ID and event ID for each booking |
| `sessions` | Hashed access/refresh tokens and their expiry times |

A user can register for several events, and an event can have several users. The `registrations` table connects them through foreign keys. Its combined primary key, `(user_id, event_id)`, prevents duplicate bookings.

## Login and roles

I used two roles: `admin` and `member`. Public signup always creates a member. Only an admin can create events or see an event's attendee list. Both roles can book seats.

Passwords are hashed with salted `scrypt`. After login, the API returns:

- An access token that lasts up to **15 minutes**.
- A refresh token for a session that lasts **seven days**.

I used random tokens instead of JWTs. The database stores only their SHA-256 hashes. When a request includes an access token, the server checks its hash and expiry, then loads the user's role.

Refreshing replaces both tokens and invalidates the previous pair. It keeps the original seven-day session expiry. Logout deletes that session, so its tokens stop working.

This keeps revocation simple, but it does mean checking the database on each authenticated request.

## How I prevent overbooking

Checking the seat count and then inserting a registration without a lock would allow two requests to see the same last seat.

I put both operations inside a transaction using `BEGIN IMMEDIATE`:

```text
Request A: lock -> check seats -> insert registration -> commit
Request B: waits -> checks the updated count -> event full -> 409
```

The lock is taken before reading the seat count. If anything fails, the transaction rolls back. Cancellation uses a write transaction too.

SQLite locks writes across the database, not just one event row. That is a trade-off I accepted to keep this project easy to run locally. For a larger application, I would consider PostgreSQL and row-level locking.

## Endpoints

| Method | Path | Access |
| --- | --- | --- |
| POST | `/auth/signup` | Public |
| POST | `/auth/login` | Valid username and password |
| POST | `/auth/refresh` | Valid refresh token |
| POST | `/auth/logout` | Refresh token identifies the session |
| GET | `/users/me` | Logged-in user |
| POST | `/events` | Admin |
| GET | `/events` | Public |
| GET | `/events/{event_id}` | Public |
| POST | `/events/{event_id}/registrations` | Logged-in user |
| DELETE | `/events/{event_id}/registrations/me` | Logged-in user |
| GET | `/users/me/registrations` | Logged-in user |
| GET | `/events/{event_id}/registrations` | Admin |

Registration and cancellation use the ID from the login session. A member cannot supply another user's ID to book or cancel on their behalf.

The event list supports pagination, title search and sorting:

```text
GET /events?page=1&page_size=10&search=Python&sort=date_asc
```

The sort options are `date_asc`, `date_desc` and `title_asc`. Page size can be between 1 and 100.

## Trying it out

A quick way to check the main flow in `/docs`:

1. Log in through `POST /auth/login` using the admin account created during setup.
2. Copy the `access_token`, click **Authorize**, and paste the token without quotes or the word `Bearer`.
3. Create an event with capacity 1 using `POST /events`. Keep its ID.
4. Sign up a member through `POST /auth/signup`, log in, and replace the token in **Authorize** with the member's access token.
5. Try creating an event as the member. It should return **403**.
6. Register for the event. The first booking returns **201**; repeating it returns **409**.
7. Sign up and log in as another member. Booking the full event should also return **409**.
8. Cancel the first member's booking using their access token. The other member can then book the seat.

Example event body:

```json
{
  "title": "Python workshop",
  "capacity": 1,
  "starts_at": "2099-01-15T10:00:00+05:30"
}
```

The start date must be in the future and include a timezone.

Example signup body (sample credentials only):

```json
{
  "username": "alice",
  "password": "ExamplePassword123!"
}
```

For refresh or logout:

```json
{
  "refresh_token": "paste_the_latest_refresh_token_here"
}
```

After refreshing, use the new access token in **Authorize**. The old tokens will no longer work.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

There are five integration tests:

1. Role restrictions, rejected admin signup attempts, password hashing and input validation.
2. Token expiry, refresh rotation, logout and two simultaneous refresh requests.
3. Duplicate bookings, cancellation ownership, foreign keys, rollback and events that have already started.
4. Eight members trying to book one seat at the same time.
5. Pagination, search, sorting and invalid query values.

All five tests passed with the versions in `requirements.txt`. In the last-seat test, exactly one request succeeds, seven get an event-full response, and the database contains one registration.

The tests use temporary databases, so they do not change the application's accounts or events. The pinned Starlette version gives a deprecation warning about the HTTPX test client, but the tests still pass.

## Project files

```text
app/
  schema.sql       Tables and relationships
  db.py            Connections and transactions
  auth.py          Password hashing and tokens
  main.py          Validation, role checks and routes
  create_admin.py  Local admin setup
tests/
  test_api.py      The five integration tests
```

I put a more detailed explanation of the logic in [my project notes](INTERVIEW_GUIDE.md).

## What I would improve next

This is a local project. Before using it as a public service, I would add HTTPS, login rate limiting, consistent login timing for unknown usernames, expired-session cleanup and database migrations. A browser frontend would also need proper token storage, such as secure HttpOnly cookies for refresh tokens.

I have not added password reset or event editing yet. The tests cover concurrent requests in one process; SQLite's file locking also coordinates writers across processes using the same local database file.

The `/docs` page loads its interface files from a CDN, so that page needs internet access. The API itself uses the local database.

## References

- [FastAPI documentation](https://fastapi.tiangolo.com/)
- [SQLite transactions](https://sqlite.org/lang_transaction.html)
- [Python scrypt documentation](https://docs.python.org/3/library/hashlib.html#hashlib.scrypt)
