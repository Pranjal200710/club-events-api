# My project notes

These are my notes on how the Club Events API works, why I used this approach, and the parts I need to be able to explain in the interview.

## 1. The idea

I made an API for a club's events. Admins create events, and members book or cancel seats. The two main things I wanted to get right were role restrictions and preventing overbooking when requests arrive together.

An API lets a client send requests to the backend and get responses. In this project, I can use the `/docs` page as the client. The data sent and received is mostly JSON.

FastAPI connects each route to a Python function. Uvicorn runs the server, and SQLite saves the data in a file.

For example, `POST /events/3/registrations` means reserving a seat at event 3 for the logged-in user. `GET` reads data, `POST` creates something or performs an action, and `DELETE` removes something.

## 2. The tables — app/schema.sql

I kept users, events and registrations in separate tables.

```text
users                 events                 registrations
id  username role     id title capacity      user_id event_id
1   admin    admin    10 Python   2          2       10
2   alice    member   11 Cloud    5          2       11
3   bob      member                          3       10
```

In this example, Alice has joined two events, and the Python event has two members. That is a many-to-many relationship.

The `registrations` table joins users and events. Each row contains one user ID and one event ID. I do not need to copy the username and event details into every booking.

A primary key identifies a record. Here, `(user_id, event_id)` is a combined primary key, so the same pair cannot be inserted twice. A foreign key makes sure the referenced user or event exists. I enable SQLite's foreign-key checks on every connection.

The `created_by` field in events also points to a real user. The role has a database check allowing only `admin` and `member`.

I use a fourth table, `sessions`, for login tokens and their expiry times.

## 3. Connections and transactions — app/db.py

I put the connection handling in `connect()` so the routes can share it.

```python
with connect(db_path, write=True) as db:
    # The write transaction has started before this block runs.
    ...
# Commit on success; roll back if an exception occurs.
```

`@contextmanager` allows the function to work with a `with` block. `yield db` passes the connection into the block. Once the block finishes, execution continues after `yield`.

A transaction groups operations together. Commit saves the changes; rollback undoes them if something fails. The `finally` block closes the connection either way.

For writes, I use `BEGIN IMMEDIATE`. It takes SQLite's write lock before reading data that is about to be changed. Other writers wait, up to the five-second timeout in this project. WAL mode allows readers to continue while a writer is working.

The lock applies to the database, not just one row. Each request can have a separate connection, and SQLite handles the coordination.

## 4. Signup and passwords — app/main.py and app/auth.py

The signup flow is:

1. FastAPI reads the JSON into the `Credentials` model.
2. Pydantic checks the username, password length and any unexpected fields.
3. The username is converted to lowercase.
4. The password is hashed with a random salt.
5. A user row is inserted with the role fixed to `member`.
6. The response contains the ID, username and role.

Invalid input gives a 422 response. A username that is already taken gives 409.

I used `hashlib.scrypt` for passwords. It is a slow password-hashing function, which makes guessing passwords more expensive if the database is exposed. The salt makes the stored values different even when two users choose the same password.

Hashing is one-way. To verify a password, the code hashes the entered password with the stored salt and compares the result using `hmac.compare_digest()`. It does not decrypt the stored hash.

Public signup cannot create an admin. Extra fields such as `"role": "admin"` are rejected, and the SQL insert explicitly sets the role to `member`. I kept admin creation in a local command that requires access to the project on the server.

## 5. Login and tokens

The login route finds the user and verifies the password. It then creates two random strings using `secrets.token_urlsafe(32)`:

- An access token, normally valid for 15 minutes.
- A refresh token, tied to a session with a seven-day lifetime.

Only the SHA-256 hashes of these tokens are stored. The actual tokens are returned to the client.

A protected request sends the access token in this header:

```text
Authorization: Bearer <access_token>
```

I used opaque tokens, meaning random strings whose details are stored in the database. JWTs work differently: they contain signed claims. With my approach, the server looks up the token hash and checks its expiry on each request.

That adds a database lookup, but it makes it straightforward to invalidate tokens by deleting the session.

### Refresh

The refresh route runs these steps inside one write transaction:

1. Find the refresh token's hash and check its expiry.
2. Delete the old session.
3. Insert a new token pair.
4. Keep the original session's seven-day expiry.
5. Commit and return the new tokens.

This is token rotation. Both old tokens stop working after refresh. Two requests trying to refresh with the same token cannot both succeed because the database lock covers the whole operation.

I do not reset the seven-day lifetime on every refresh. Otherwise, repeated refreshing could keep a session alive indefinitely.

### Logout

Logout accepts the current refresh token and deletes its session. Both tokens from that session then stop working. It still works if the access token has already expired. Other login sessions are unaffected.

### Why two different hash functions?

Passwords can be short or predictable, so I use slow scrypt hashing. Tokens are long random values, so SHA-256 is suitable for looking them up without storing the raw secrets.

## 6. Authentication and authorization

I think of these as two separate checks:

- Authentication: identify the user.
- Authorization: check whether that user is allowed to perform the action.

`current_user()` checks the access token and loads the user. `admin_only()` then checks whether their role is `admin`.

FastAPI's `Depends()` runs these checks before the route. The `User` and `Admin` aliases let me reuse the checks across endpoints.

The role comes from the database, not from a request body. For registration and cancellation, the user ID also comes from the session. Alice cannot enter Bob's ID to cancel his booking.

The response codes I use are:

| Code | Meaning here |
| --- | --- |
| 201 | A user, event or registration was created |
| 204 | Cancellation or logout completed with no response body |
| 401 | Missing, invalid or expired login token |
| 403 | Valid login, but the role is not allowed |
| 404 | The event or the user's registration does not exist |
| 409 | A conflict, such as a full event or duplicate booking |
| 422 | Invalid input |
| 503 | The database stayed busy beyond the timeout |

## 7. Events, search and pagination

`EventInput` checks the event title, capacity and start date. Capacity must be a positive integer, and the date must be in the future with a timezone. I convert dates to UTC before storing them.

Only an admin can call `create_event()`. To show an event's remaining seats, I count its registrations:

```text
seats_left = capacity - registered_count
```

I calculate this count instead of saving another counter that would need to be updated after every booking and cancellation.

The event list accepts query parameters:

```text
GET /events?page=2&page_size=10&search=Python&sort=date_asc
```

`LIMIT` controls how many rows are returned. `OFFSET` skips earlier rows:

```text
offset = (page - 1) * page_size
page 2 with 10 items per page -> skip the first 10
```

Search uses a parameterized `LIKE` query. The `?` placeholders keep supplied values separate from SQL instructions. For sorting, I only allow three predefined options, so arbitrary text cannot become part of `ORDER BY`.

I use a read transaction for the total count and the page query. This keeps the two results consistent if another request creates an event in between.

## 8. The last-seat problem

Without a lock, two requests could do this:

```text
There is one seat left.
Alice checks: one seat available.
Bob checks: one seat available.
Alice inserts a registration.
Bob inserts a registration.
The event is now overbooked.
```

This is a race condition: the outcome depends on how operations overlap.

In `register()`, I do the following:

1. Check the user's access token.
2. Start a write transaction and acquire the database lock.
3. Check that the event exists and has not started.
4. Check whether the user is already registered.
5. Count the registrations and compare the count with capacity.
6. Insert the registration, then commit.

Bob's write transaction waits while Alice holds the lock. Once Alice commits, Bob reads the updated count and gets a 409 response if the event is full.

The `if` check is safe because the lock is already held when the count is read. Starting a transaction only after reading the count would be too late.

I used a database lock rather than a Python `threading.Lock`, since a Python lock would only coordinate callers sharing that lock in one process. SQLite's file locking also works between processes using the same local database file.

SQLite still allows only one writer at a time across the database. If the app needed to handle many writes to different events, I would consider PostgreSQL with a lock on the specific event row.

## 9. The five tests — tests/test_api.py

I used integration tests so the routes, authentication and database are checked together.

The pytest fixture creates a temporary database, starts a test app and seeds an admin account. Each test gets its own database, separate from `data/club.db`.

The tests cover:

1. Roles, password hashing and validation.
2. Token expiry, rotation, logout and simultaneous refresh requests.
3. Relationships, duplicates, cancellation ownership and rollback.
4. Multiple users competing for the last seat.
5. Search, pagination, sorting and invalid query values.

For the last-seat test, eight users try to register for an event with capacity one. `ThreadPoolExecutor` runs the callers, and `threading.Barrier` waits until all are ready before releasing them together.

Each caller uses a separate test client, and the routes open separate database connections. The expected result is one 201 response, seven 409 responses and exactly one registration in the database.

The refresh test uses the same idea with two requests sharing one refresh token. Exactly one gets a new token pair.

The rollback test inserts an event, then deliberately causes a foreign-key failure in the same transaction. It checks that the earlier insert was undone. The pagination test checks the actual returned titles and page boundaries.

## 10. Python syntax notes

| Syntax | What it does in this project |
| --- | --- |
| `def` / `return` | Defines a function / returns its result |
| `@app.post(...)` | Connects a function to an HTTP route |
| `BaseModel` | Defines the expected request body |
| `Depends(...)` | Runs a shared check before the route |
| `with` | Handles setup and cleanup around a block |
| `yield` in `connect()` | Passes the connection into the block |
| `try / except / finally` | Handles failures and cleanup |
| `raise HTTPException(...)` | Stops a request with an error response |
| `fetchone()` / `fetchall()` | Reads one row / all matching rows |
| `if __name__ == "__main__"` | Runs the admin command when the module is executed directly |

The routes use normal `def` because the SQLite operations are synchronous. FastAPI runs these handlers in its worker thread pool. The startup `lifespan` function uses an async context manager to initialize the database when the app starts.

## 11. My interview checklist

Before the interview, I want to be able to:

- Run the app and demonstrate an admin and a member account.
- Explain how the three main tables are related.
- Trace a registration from the incoming request to the database commit.
- Explain access versus refresh tokens and 401 versus 403.
- Show why reading the seat count before taking the lock would be unsafe.
- Explain what each test checks, especially the concurrent booking test.
- Make a small change, such as adding a sort option, and check that it works.

The main files for these points are `schema.sql`, `db.py`, `auth.py`, `main.py` and `test_api.py`.

## References

- [SQLite transactions](https://sqlite.org/lang_transaction.html)
- [FastAPI dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/)
- [Python scrypt](https://docs.python.org/3/library/hashlib.html#hashlib.scrypt)
- [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/)
