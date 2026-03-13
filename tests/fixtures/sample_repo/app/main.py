"""Main application module with deliberate issues for testing."""

import sqlite3


def get_db():
    return sqlite3.connect(":memory:")


def get_user(user_id):
    """SQL injection vulnerability: raw string formatting in query."""
    db = get_db()
    cursor = db.cursor()
    query = f"SELECT * FROM users WHERE id = '{user_id}'"  # noqa: S608
    cursor.execute(query)
    return cursor.fetchone()


def format_user_report(user):
    """Duplicated reporting logic (copy 1)."""
    if user is None:
        return "No user found"
    name = user[1]
    email = user[2]
    status = user[3]
    lines = []
    lines.append(f"=== User Report ===")
    lines.append(f"Name:   {name}")
    lines.append(f"Email:  {email}")
    lines.append(f"Status: {status}")
    lines.append(f"Active: {'Yes' if status == 'active' else 'No'}")
    lines.append(f"Level:  {'Admin' if status == 'admin' else 'User'}")
    lines.append(f"---")
    lines.append(f"Generated for {name}")
    return "\n".join(lines)


def format_admin_report(user):
    """Duplicated reporting logic (copy 2) — nearly identical to format_user_report."""
    if user is None:
        return "No user found"
    name = user[1]
    email = user[2]
    status = user[3]
    lines = []
    lines.append(f"=== Admin Report ===")
    lines.append(f"Name:   {name}")
    lines.append(f"Email:  {email}")
    lines.append(f"Status: {status}")
    lines.append(f"Active: {'Yes' if status == 'active' else 'No'}")
    lines.append(f"Level:  {'Admin' if status == 'admin' else 'User'}")
    lines.append(f"---")
    lines.append(f"Generated for {name}")
    return "\n".join(lines)


def add(a, b):
    """Simple utility — tested below."""
    return a + b
