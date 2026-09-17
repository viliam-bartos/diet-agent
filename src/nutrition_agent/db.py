"""Database connection and schema initialization for SQLite."""

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS food_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    raw_input TEXT NOT NULL,
    food_name_matched TEXT NOT NULL,
    amount_g REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('raw', 'cooked')),
    is_estimated INTEGER NOT NULL CHECK (is_estimated IN (0, 1)),
    calories REAL NOT NULL,
    protein_raw REAL NOT NULL,
    protein_usable REAL NOT NULL,
    carbs REAL NOT NULL,
    fats REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_food_logs_logged_at ON food_logs(logged_at);
CREATE INDEX IF NOT EXISTS idx_food_logs_estimated ON food_logs(is_estimated);

CREATE TABLE IF NOT EXISTS weight_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    raw_input TEXT NOT NULL,
    weight_kg REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_weight_logs_logged_at ON weight_logs(logged_at);

CREATE TABLE IF NOT EXISTS user_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workout_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    raw_input TEXT NOT NULL,
    workout_type TEXT NOT NULL,
    duration_minutes INTEGER,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_workout_logs_logged_at ON workout_logs(logged_at);
"""


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Create and return a configured SQLite connection."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: str | Path) -> None:
    """Initialize SQLite database tables and indices."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
