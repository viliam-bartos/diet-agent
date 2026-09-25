"""Database connection and schema initialization for SQLite."""

import sqlite3
from pathlib import Path

from nutrition_agent.models import food_name_key

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
    fats REAL NOT NULL,
    alcohol REAL NOT NULL DEFAULT 0,
    nutrition_source TEXT NOT NULL DEFAULT 'curated',
    conversion_estimated INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS day_types (
    date TEXT PRIMARY KEY,
    day_type TEXT NOT NULL CHECK (day_type IN ('training', 'rest')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS foods (
    name TEXT PRIMARY KEY,
    name_key TEXT NOT NULL UNIQUE,
    calories REAL NOT NULL,
    protein REAL NOT NULL,
    diaas_coefficient REAL NOT NULL,
    carbs REAL NOT NULL,
    fats REAL NOT NULL,
    alcohol REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL CHECK (source IN ('curated', 'ai')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_foods_source ON foods(source);

CREATE TABLE IF NOT EXISTS unit_conversions (
    food_key TEXT NOT NULL,
    unit TEXT NOT NULL,
    grams_per_unit REAL NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('user', 'ai')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (food_key, unit)
);
"""

# Kept separate so the migration can rebuild the table with the same definition.
FOODS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS foods (
    name TEXT PRIMARY KEY,
    name_key TEXT NOT NULL UNIQUE,
    calories REAL NOT NULL,
    protein REAL NOT NULL,
    diaas_coefficient REAL NOT NULL,
    carbs REAL NOT NULL,
    fats REAL NOT NULL,
    alcohol REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL CHECK (source IN ('curated', 'ai')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_foods_source ON foods(source);
"""

# Columns added after the first release, applied to databases created by older versions.
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("food_logs", "nutrition_source", "TEXT NOT NULL DEFAULT 'curated'"),
    ("food_logs", "conversion_estimated", "INTEGER NOT NULL DEFAULT 0"),
    ("food_logs", "alcohol", "REAL NOT NULL DEFAULT 0"),
    ("foods", "alcohol", "REAL NOT NULL DEFAULT 0"),
)


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Create and return a configured SQLite connection."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names currently present in a table."""
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Report whether a table exists in the database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    """Add columns that older database files are missing."""
    for table, column, definition in COLUMN_MIGRATIONS:
        if _table_exists(conn, table) and column not in _existing_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_foods_lookup_key(conn: sqlite3.Connection) -> None:
    """Rebuild the foods table when it predates the normalized name_key column.

    A UNIQUE NOT NULL column cannot be added in place, so the table is rebuilt.
    Keys are recomputed in Python because SQLite's lower() is ASCII-only.
    """
    if not _table_exists(conn, "foods") or "name_key" in _existing_columns(conn, "foods"):
        return

    legacy_rows = conn.execute(
        """
        SELECT name, calories, protein, diaas_coefficient, carbs, fats, source,
               created_at, updated_at
        FROM foods
        """
    ).fetchall()

    conn.execute("ALTER TABLE foods RENAME TO foods_legacy")
    conn.executescript(FOODS_TABLE_SQL)

    for row in legacy_rows:
        conn.execute(
            """
            INSERT INTO foods
                (name, name_key, calories, protein, diaas_coefficient, carbs, fats,
                 source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name_key) DO NOTHING
            """,
            (
                row["name"],
                food_name_key(row["name"]),
                row["calories"],
                row["protein"],
                row["diaas_coefficient"],
                row["carbs"],
                row["fats"],
                row["source"],
                row["created_at"],
                row["updated_at"],
            ),
        )

    conn.execute("DROP TABLE foods_legacy")


def _migrate_stored_day_type(conn: sqlite3.Connection) -> None:
    """Move the old global day_type setting onto the date it was written.

    The setting used to be one value for all days, so a workout logged on Monday made
    Tuesday look like a training day too. The value belongs to the day it was chosen on;
    every later day starts without one.
    """
    if not _table_exists(conn, "user_settings") or not _table_exists(conn, "day_types"):
        return

    legacy = conn.execute(
        "SELECT value, updated_at FROM user_settings WHERE key = 'day_type'"
    ).fetchone()
    if legacy is None or legacy["value"] not in ("training", "rest"):
        return

    conn.execute(
        """
        INSERT INTO day_types (date, day_type, updated_at)
        VALUES (date(?, 'localtime'), ?, ?)
        ON CONFLICT(date) DO NOTHING
        """,
        (legacy["updated_at"], legacy["value"], legacy["updated_at"]),
    )
    conn.execute("DELETE FROM user_settings WHERE key = 'day_type'")


def _drop_redundant_day_types(conn: sqlite3.Connection) -> None:
    """Remove day-type rows that only repeat what the logs already say.

    Logging a workout used to store the day type as well, so deleting the workout left
    the day announced as a training day. Such a row carries nothing the workout log does
    not already hold, so it is dropped here and the day follows its records again.
    """
    if not _table_exists(conn, "day_types") or not _table_exists(conn, "workout_logs"):
        return

    conn.execute(
        """
        DELETE FROM day_types
        WHERE (day_type = 'training' AND EXISTS (
                   SELECT 1 FROM workout_logs
                   WHERE date(logged_at, 'localtime') = day_types.date))
           OR (day_type = 'rest' AND NOT EXISTS (
                   SELECT 1 FROM workout_logs
                   WHERE date(logged_at, 'localtime') = day_types.date))
        """
    )


def init_db(db_path: str | Path) -> None:
    """Initialize SQLite tables, indices and pending migrations."""
    with get_connection(db_path) as conn:
        _migrate_foods_lookup_key(conn)
        conn.executescript(SCHEMA_SQL)
        _apply_column_migrations(conn)
        _migrate_stored_day_type(conn)
        _drop_redundant_day_types(conn)
