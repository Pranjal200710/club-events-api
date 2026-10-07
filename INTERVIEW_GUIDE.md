# Understand your Club Events API

Read this with the code open. The goal is to be able to trace one request and explain why each check exists. You do not need to memorize library internals.

## 1. What did you build?

Say it in your own words:

> I built an API for a club's events. Admins create events, and members reserve or cancel seats. Users and events are connected through a registrations table. Login gives short-lived access tokens and refresh tokens. The important part is that registration uses a database transaction, so two requests cannot overbook the last seat.

An **API** is a set of rules for sending requests to a program and getting responses. Your browser, a mobile app, or a tool such as Postman can be the client. The **backend** processes those requests. JSON is the text format we use to exchange data.

FastAPI matches each request to a Python function. Uvicorn is the server that accepts the network connection. SQLite stores data in a file that survives a restart.

Example: `POST /events/3/registrations` means "reserve a seat for the logged-in user at event number 3." `POST` creates something; `GET` reads something; `DELETE` removes something.

## 2. Understand the tables first — app/schema.sql

Imagine these records:

```text
users                 events                 registrations
id  username role     id title capacity      user_id event_id
1   admin    admin    10 Python   2          2       10
2   alice    member   11 Cloud    5          2       11
3   bob      member                          3       10
```

Alice is in two events. The Python event has two users. This is a **many-to-many relationship**: one user can join many events, and one event can have many users.

The registrations table is a **join table**. Each row connects one user ID and one event ID. It avoids copying usernames or event titles into every registration.

A **primary key** uniquely identifies a record. The registrations table has a combined primary key `(user_id, event_id)`, so `(2, 10)` cannot appear twice. A **foreign key** requires the referenced user/event to exist. `PRAGMA foreign_keys = ON` enables enforcement on every SQLite connection.

`events.created_by` also references a real user. SQL's role `CHECK` allows only `admin` and `member`. Python validation rejects bad inputs before SQL, and database constraints protect the stored data as a second layer.

The fourth table, `sessions`, stores login sessions. It is part of authentication, not a substitute for the relationship between users and events.

## 3. Understand connections and transactions — app/db.py

`connect()` opens a connection, lets the caller use it, then closes it. `@contextmanager` makes it usable with Python's `with` statement. `yield db` hands the connection to the code inside that block. After the block finishes, execution resumes after `yield`.

```python
with connect(db_path, write=True) as db:
    # Use db here. The transaction has already started.
    ...
# The transaction committed, and the connection closed.
```

A **transaction** groups work into one unit. **Commit** makes it permanent. **Rollback** cancels changes if an exception occurs. `try / except / finally` means: attempt the work, handle a failure, and always clean up.

`BEGIN IMMEDIATE` is the critical line. SQLite acquires its write lock before our code reads a seat count. Other writers wait (up to five seconds in our configuration). Read operations can continue using WAL mode. The lock is database-wide, not a row lock.

Every request can use a different connection. The database coordinates them. A normal Python `if` statement by itself would not coordinate two requests.

## 4. Signup and passwords — app/main.py and app/auth.py

Follow `signup()`:

1. FastAPI reads the request's JSON into the `Credentials` model.
2. Pydantic checks username/password length, permitted username characters, and unexpected fields. A bad body returns 422.
3. The username is normalized to lowercase.
4. `hash_password()` generates a random salt and uses scrypt to derive a password hash.
5. SQL inserts a user with the literal role `'member'`.
6. The response returns the ID, username and role. It never returns the password hash.

**Why not store passwords directly?** If someone reads the database, plain passwords would immediately be exposed. A slow password hash makes guessing more expensive. The random salt means two people with the same password have different stored values.

Hashing is one-way. Encryption is reversible with a key. We verify a password by running the same hashing function with the stored salt and comparing the result. `hmac.compare_digest()` is designed for constant-time comparisons.

You do not need to derive the scrypt algorithm in an interview. Know its purpose and that the standard library handles its implementation.

**Why can't I sign up as an admin?** The input model rejects extra fields such as `role`, and the SQL fixes new users to `member`. Admins are created through a trusted local command with server access. That command is not a public API endpoint.

## 5. Login, access tokens, refresh tokens and logout

Follow `login()`:

1. Find the username in the database.
2. Verify the supplied password against the stored hash.
3. Generate two unpredictable random strings using `secrets.token_urlsafe(32)`.
4. Store only their SHA-256 hashes and expiry times in `sessions`.
5. Return the raw strings once to the client.

An **access token** is a temporary pass. The client sends it with each protected request:

```text
Authorization: Bearer <access_token>
```

It normally lasts 15 minutes. A **refresh token** can get a new token pair for the same session without asking for the password again. The session has a maximum seven-day lifetime.

These are **opaque tokens**: random strings whose meaning lives in the database. They are not JWTs. JWTs contain signed claims; our server instead looks up a token hash. Both formats can support expiring access tokens. Our choice makes revocation easy, at the cost of a database lookup on each protected request.

Follow `refresh()`:

1. Begin a write transaction.
2. Find the refresh token's hash and check that it has not expired.
3. Delete the old session row.
4. Insert a new token pair, keeping the original session expiry.
5. Commit and return the new tokens.

This is **refresh token rotation**. After refresh, both old tokens are invalid. Two simultaneous refresh attempts cannot both succeed, because reading and replacing the token are inside one locked transaction. Reusing an old token returns 401.

`logout()` deletes the session identified by the refresh token. Both its access and refresh token stop working for subsequent checks. It works even when the access token has expired. Other independently logged-in sessions are unaffected.

**Why SHA-256 for tokens but scrypt for passwords?** Tokens are already very long, random secrets. Passwords are human-chosen and guessable, so they need an intentionally expensive hash.

## 6. Authentication versus authorization

**Authentication** asks "Who are you?" `current_user()` reads the bearer token, hashes it, checks its expiry and loads the user.

**Authorization** asks "Are you allowed to do this?" `admin_only()` checks the user's role. A member has a valid login but still cannot create an event.

`Depends(current_user)` tells FastAPI to run that function before the route. `Admin` includes a further `admin_only` check. This shares the checks across routes so we do not copy them into every function.

Remember the difference:

- **401**: no valid login token.
- **403**: logged in, but wrong role.
- **404**: requested event or own registration does not exist.
- **409**: request conflicts with current state, such as a full event.
- **422**: invalid input, such as zero capacity.
- **503**: database stayed busy longer than the configured timeout; retry later.

The role is read from the database, not trusted from a request body. Cancellation uses the authenticated user ID, so Alice cannot choose Bob's ID and cancel his seat.

## 7. Creating and listing events

`EventInput` validates the title, capacity, and timezone-aware start date. The event must start in the future. We convert times to UTC before storing them so ordering and comparisons are consistent.

`create_event()` requires an admin and inserts the event. `get_event()` joins registrations and counts them. `seats_left = capacity - registered_count`. We calculate the count instead of maintaining a second counter that might go out of sync.

`list_events()` accepts page, page size, search, and sort values:

```text
GET /events?page=2&page_size=10&search=Python&sort=date_asc
```

**Pagination** splits results into pages. `LIMIT 10` returns at most 10 rows. `OFFSET (page - 1) * page_size` skips earlier rows: page 2 skips 10.

Search uses a parameterized `LIKE` query. Sort values are selected from a fixed dictionary of safe SQL fragments. Never put arbitrary user-provided text directly into SQL. SQL placeholders (`?`) keep user data separate from the query's instructions.

The response includes `total` as well as the current page. A read transaction keeps both queries on the same snapshot if an event is created between them.

## 8. The most important explanation: no overbooking

Without a lock, this can happen:

```text
One seat left.
Alice reads: 1 seat left.
Bob reads:   1 seat left.
Alice inserts a registration.
Bob inserts a registration.
Two registrations were made for one seat.
```

That is a **race condition**. The result depends on how operations overlap in time.

Follow `register()` in our implementation:

1. Authenticate the user.
2. Enter `connect(..., write=True)`, which acquires SQLite's write lock.
3. Check that the event exists and has not started.
4. Check that this user is not already registered.
5. Count registrations and check capacity, while still holding the lock.
6. Insert a registration, then commit and release the lock.

While Alice is doing steps 3–6, Bob cannot start another write transaction. When Alice commits, Bob proceeds and sees the updated count. He receives 409, event full.

**Is the `if` statement safe here?** Yes, because it runs inside a database transaction that already holds the write lock. It would be unsafe if we read the count first and only started the transaction afterwards.

**Why not use `threading.Lock`?** A Python lock only protects the code sharing that lock in one process. SQLite's file locks coordinate separate database connections, including separate server processes on the same machine.

**Would you use this at huge scale?** SQLite serializes all writes. A larger service could use PostgreSQL and lock the target event row with `SELECT ... FOR UPDATE` inside a transaction. We chose SQLite to keep local setup simple.

## 9. Understand the five tests — tests/test_api.py

An integration test checks multiple pieces together. These tests send requests through FastAPI and use real temporary SQLite database files. They do not just check for HTTP 200.

`@pytest.fixture` prepares a fresh app, database, test client and admin for each test. `tmp_path` is a temporary directory supplied by pytest. `assert` expresses the result that must be true.

The last-seat test creates eight users and one event with capacity one. `ThreadPoolExecutor` runs eight callers. `threading.Barrier` makes them wait until all are ready before releasing their registration requests together. Each caller uses a separate HTTP test client; routes open separate database connections.

The test checks both responses and stored state: one success, seven full-event responses, and exactly one registration row. The refresh test similarly races two attempts using the same refresh token and expects one winner.

The relationship test deliberately causes a foreign-key failure after an insertion in a transaction and verifies that the insertion was rolled back. The pagination test checks actual returned titles and page boundaries. These prove business rules rather than just server availability.

## 10. Python syntax you should recognize

| Syntax | Meaning in this project |
| --- | --- |
| `def` / `return` | Define a function / send back its result |
| `@app.post(...)` | Register a function as an HTTP route |
| `class ... (BaseModel)` | Declare the shape and rules of a request body |
| `Depends(...)` | Ask FastAPI to run a prerequisite function |
| `with` | Use a resource and reliably clean it up |
| `try / except / finally` | Attempt work, handle errors, always clean up |
| `raise HTTPException(...)` | Stop the request and send an error response |
| `?` in SQL | A placeholder for safely supplied data |
| `fetchone()` / `fetchall()` | Read one matching row / all matching rows |
| `if __name__ == "__main__"` | Run the command only when that module is executed directly |

Routes use normal `def` because SQLite operations are synchronous. FastAPI runs these handlers in a worker thread pool. The startup `lifespan` is an async context manager because that is FastAPI's startup/shutdown interface; the business logic does not require learning async programming first.

## 11. Practice before the interview

1. Run the app, create an admin, and complete the README's demo yourself.
2. Explain the users/events/registrations example without reading.
3. Trace one registration from token checking to transaction commit.
4. Explain 401 versus 403, access versus refresh, and commit versus rollback.
5. Run the tests and describe what the concurrency test proves.
6. Make a small change yourself: change an error message or add a new allowed sort option, then verify it.

If they ask whether you used AI, answer honestly: you used it to help implement the project, then studied the design, ran the tests, and practiced the flows. Be ready to point to the exact code for each claim. Reading this guide once is preparation; being able to modify and explain the code is the real check.

## References

- [SQLite transactions and BEGIN IMMEDIATE](https://sqlite.org/lang_transaction.html)
- [FastAPI dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/)
- [Python scrypt documentation](https://docs.python.org/3/library/hashlib.html#hashlib.scrypt)
- [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/)
