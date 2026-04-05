"""
Database layer — SQLite, fully local.
Tables:
  expenses     — every logged expense
  categories   — category definitions + budgets
  digests      — weekly digest archive
"""

import sqlite3
import json
from datetime import datetime, date
from pathlib import Path

DB_PATH = Path(__file__).parent / "expenses.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            date        TEXT    NOT NULL,
            amount      REAL    NOT NULL,
            currency    TEXT    DEFAULT 'EUR',
            vendor      TEXT,
            category    TEXT,
            subcategory TEXT,
            description TEXT,
            source      TEXT,
            raw_text    TEXT,
            confirmed   INTEGER DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT NOT NULL UNIQUE,
            budget  REAL,
            emoji   TEXT DEFAULT '💰'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS digests (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            period  TEXT,
            content TEXT
        )
    """)

    # Seed default categories if empty
    c.execute("SELECT COUNT(*) FROM categories")
    if c.fetchone()[0] == 0:
        defaults = [
            ("Food & Dining",     600,  "🍽️"),
            ("Groceries",         400,  "🛒"),
            ("Transport",         200,  "🚗"),
            ("Health",            150,  "💊"),
            ("Shopping",          300,  "🛍️"),
            ("Entertainment",     150,  "🎬"),
            ("Subscriptions",     100,  "📱"),
            ("Utilities",         200,  "💡"),
            ("Travel",            500,  "✈️"),
            ("Education",         100,  "📚"),
            ("Personal Care",     100,  "🪥"),
            ("Other",             200,  "📦"),
        ]
        c.executemany(
            "INSERT INTO categories (name, budget, emoji) VALUES (?,?,?)",
            defaults
        )

    conn.commit()
    conn.close()
    print("[DB] Initialised expenses.db")


# ── Expenses ──────────────────────────────────────────────────────────────────

def log_expense(amount: float, vendor: str = None, category: str = None,
                description: str = None, date_str: str = None,
                currency: str = "EUR", source: str = "manual",
                raw_text: str = None, confirmed: int = 1) -> int:
    conn = get_conn()
    c    = conn.cursor()
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    c.execute("""
        INSERT INTO expenses
            (ts, date, amount, currency, vendor, category, description,
             source, raw_text, confirmed)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now().isoformat(), date_str, amount, currency,
          vendor, category, description, source, raw_text, confirmed))
    row_id = c.lastrowid
    conn.commit()
    conn.close()
    return row_id


def get_expenses(days: int = 30, category: str = None) -> list:
    conn = get_conn()
    query = """
        SELECT * FROM expenses
        WHERE date >= date('now', ?)
        AND confirmed = 1
    """
    params = [f"-{days} days"]
    if category:
        query  += " AND category = ?"
        params.append(category)
    query += " ORDER BY date DESC"
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    return rows


def get_expenses_by_month(year: int, month: int) -> list:
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("""
        SELECT * FROM expenses
        WHERE strftime('%Y', date) = ?
        AND   strftime('%m', date) = ?
        AND   confirmed = 1
        ORDER BY date DESC
    """, (str(year), f"{month:02d}")).fetchall()]
    conn.close()
    return rows


def get_monthly_totals(months: int = 6) -> list:
    """Return total per month for the last N months."""
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("""
        SELECT
            strftime('%Y-%m', date) AS month,
            SUM(amount)             AS total,
            COUNT(*)                AS count
        FROM expenses
        WHERE date >= date('now', ?)
        AND   confirmed = 1
        GROUP BY month
        ORDER BY month DESC
    """, (f"-{months * 31} days",)).fetchall()]
    conn.close()
    return rows


def get_category_totals(days: int = 30) -> list:
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("""
        SELECT
            COALESCE(category, 'Uncategorised') AS category,
            SUM(amount)  AS total,
            COUNT(*)     AS count,
            AVG(amount)  AS avg
        FROM expenses
        WHERE date >= date('now', ?)
        AND   confirmed = 1
        GROUP BY category
        ORDER BY total DESC
    """, (f"-{days} days",)).fetchall()]
    conn.close()
    return rows


def get_categories() -> list:
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM categories ORDER BY name"
    ).fetchall()]
    conn.close()
    return rows


def delete_expense(expense_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    conn.commit()
    conn.close()


def update_expense_category(expense_id: int, category: str):
    conn = get_conn()
    conn.execute("UPDATE expenses SET category=? WHERE id=?", (category, expense_id))
    conn.commit()
    conn.close()


def set_budget(category: str, budget: float):
    conn = get_conn()
    conn.execute("""
        INSERT INTO categories (name, budget) VALUES (?,?)
        ON CONFLICT(name) DO UPDATE SET budget=excluded.budget
    """, (category, budget))
    conn.commit()
    conn.close()


def log_digest(period: str, content: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO digests (ts, period, content) VALUES (?,?,?)",
        (datetime.now().isoformat(), period, content)
    )
    conn.commit()
    conn.close()

def get_uncategorised(limit: int = 100) -> list:
    """Return expenses with no category or category = Uncategorised."""
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("""
        SELECT * FROM expenses
        WHERE (category IS NULL OR category = 'Uncategorised' OR category = '')
        AND confirmed = 1
        ORDER BY date DESC
        LIMIT ?
    """, (limit,)).fetchall()]
    conn.close()
    return rows


def bulk_update_categories(updates: list):
    """
    Bulk update categories. updates = list of (category, expense_id) tuples.
    """
    conn = get_conn()
    conn.executemany(
        "UPDATE expenses SET category=? WHERE id=?", updates
    )
    conn.commit()
    conn.close()


# ── Vendor rules (learned from user) ─────────────────────────────────────────

def init_vendor_rules(conn=None):
    close = conn is None
    if conn is None:
        conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vendor_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_key  TEXT NOT NULL UNIQUE,
            category    TEXT NOT NULL,
            always      INTEGER DEFAULT 1,
            ts          TEXT NOT NULL
        )
    """)
    if close:
        conn.commit()
        conn.close()


def get_vendor_rule(vendor: str) -> str | None:
    """Return learned category for this vendor, or None."""
    try:
        conn = get_conn()
        init_vendor_rules(conn)
        row = conn.execute(
            "SELECT category FROM vendor_rules WHERE vendor_key = ?",
            (_vendor_key(vendor),)
        ).fetchone()
        conn.commit()
        conn.close()
        return row["category"] if row else None
    except Exception:
        return None


def save_vendor_rule(vendor: str, category: str, always: bool = True):
    """Save a vendor → category rule learned from the user."""
    conn = get_conn()
    init_vendor_rules(conn)
    conn.execute("""
        INSERT INTO vendor_rules (vendor_key, category, always, ts)
        VALUES (?,?,?,?)
        ON CONFLICT(vendor_key) DO UPDATE SET
            category=excluded.category,
            always=excluded.always,
            ts=excluded.ts
    """, (_vendor_key(vendor), category, int(always), __import__('datetime').datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_all_vendor_rules() -> list:
    conn = get_conn()
    init_vendor_rules(conn)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM vendor_rules ORDER BY vendor_key"
    ).fetchall()]
    conn.close()
    return rows


def _vendor_key(vendor: str) -> str:
    """Normalise vendor name for consistent matching."""
    import re
    return re.sub(r"\s+", " ", vendor.lower().strip())

def get_last_expenses(limit: int = 10) -> list:
    """Return the last N confirmed expenses."""
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("""
        SELECT * FROM expenses
        WHERE confirmed = 1
        ORDER BY date DESC, id DESC
        LIMIT ?
    """, (limit,)).fetchall()]
    conn.close()
    return rows