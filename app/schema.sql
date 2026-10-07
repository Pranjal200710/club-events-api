CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    starts_at TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id)
);

-- This join table expresses the many-to-many relationship.
-- The composite primary key also prevents duplicate registrations.
CREATE TABLE IF NOT EXISTS registrations (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, event_id)
);
CREATE INDEX IF NOT EXISTS registrations_by_event ON registrations(event_id);

-- Only hashes of random tokens are stored, never the raw tokens.
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    access_hash TEXT NOT NULL UNIQUE,
    refresh_hash TEXT NOT NULL UNIQUE,
    access_expires_at INTEGER NOT NULL,
    refresh_expires_at INTEGER NOT NULL
);
