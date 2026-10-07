"""Password hashing and expiring, database-backed bearer tokens."""

import hashlib
import hmac
import secrets
import time

ACCESS_SECONDS = 15 * 60
REFRESH_SECONDS = 7 * 24 * 60 * 60


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=2**15, r=8, p=3,
        maxmem=64 * 1024 * 1024,
    ).hex()
    return f"{salt}:{digest}"


def verify_password(password, stored_hash):
    salt, expected = stored_hash.split(":")
    actual = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=2**15, r=8, p=3,
        maxmem=64 * 1024 * 1024,
    ).hex()
    return hmac.compare_digest(actual, expected)


def token_hash(token):
    # Random 256-bit tokens need a fast hash; human passwords need a slow hash.
    return hashlib.sha256(token.encode()).hexdigest()


def issue_tokens(db, user_id, *, refresh_expires_at=None):
    """Caller holds the write transaction. Return raw tokens only to the client."""
    now = int(time.time())
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    # Refresh rotation does not extend the session's original seven-day lifetime.
    if refresh_expires_at is None:
        refresh_expires_at = now + REFRESH_SECONDS
    access_expires_at = min(now + ACCESS_SECONDS, refresh_expires_at)
    db.execute(
        """INSERT INTO sessions
           (user_id, access_hash, refresh_hash, access_expires_at, refresh_expires_at)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, token_hash(access), token_hash(refresh),
         access_expires_at, refresh_expires_at),
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": access_expires_at - now,
        "refresh_expires_in": refresh_expires_at - now,
    }
