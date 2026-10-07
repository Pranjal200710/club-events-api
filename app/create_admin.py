"""Run locally to create an administrator. Public signup cannot create admins."""

import getpass
import sqlite3

from pydantic import ValidationError

from .auth import hash_password
from .db import DEFAULT_DB, connect, init_db
from .main import Credentials


def main():
    print("Create a local admin account (password input is hidden).")
    try:
        credentials = Credentials(username=input("Username: "), password=getpass.getpass("Password: "))
    except ValidationError:
        print("Use a 3-30 character username (letters/numbers/underscore) and an 8-128 character password.")
        return 1
    if credentials.password != getpass.getpass("Confirm password: "):
        print("Passwords do not match.")
        return 1
    init_db(DEFAULT_DB)
    password_hash = hash_password(credentials.password)
    try:
        with connect(DEFAULT_DB, write=True) as db:
            db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                (credentials.username, password_hash),
            )
    except sqlite3.IntegrityError:
        print("Username already exists. Choose a new admin username.")
        return 1
    print(f"Admin '{credentials.username}' created. You can now log in at /docs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
