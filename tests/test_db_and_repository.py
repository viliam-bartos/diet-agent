"""Tests for SQLite database schema and repository persistence."""

import sqlite3

import pytest

from nutrition_agent.db import get_connection, init_db
from nutrition_agent.models import CalculatedFoodLog, CalculatedWeightLog
from nutrition_agent.repository import (
    get_recent_food_logs,
    get_recent_weight_logs,
    load_foods_from_json,
    save_food_logs,
    save_weight_log,
)


@pytest.fixture
def mem_db() -> sqlite3.Connection:
    """Fixture providing initialized in-memory SQLite connection."""
    conn = get_connection(":memory:")
    init_db(":memory:")  # verify init_db executes cleanly
    # Apply schema to this in-memory connection
    from nutrition_agent.db import SCHEMA_SQL

    conn.executescript(SCHEMA_SQL)
    return conn


def test_init_db_creates_tables_and_indices(mem_db: sqlite3.Connection) -> None:
    """Verify tables food_logs and weight_logs are created."""
    cursor = mem_db.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('food_logs', 'weight_logs')"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert "food_logs" in tables
    assert "weight_logs" in tables


def test_save_and_retrieve_food_logs(mem_db: sqlite3.Connection) -> None:
    """Verify persisting food logs and reading them back."""
    log = CalculatedFoodLog(
        raw_input="200g kuřecí prsa",
        food_name_matched="Kuřecí prsa",
        amount_g=200.0,
        state="raw",
        is_estimated=False,
        calories=212.0,
        protein_raw=46.0,
        protein_usable=46.0,
        carbs=0.0,
        fats=2.4,
    )

    saved = save_food_logs(mem_db, [log])
    assert len(saved) == 1
    assert saved[0].id is not None

    recent = get_recent_food_logs(mem_db, limit=10)
    assert len(recent) == 1
    assert recent[0].food_name_matched == "Kuřecí prsa"
    assert recent[0].amount_g == 200.0
    assert recent[0].calories == 212.0
    assert recent[0].protein_usable == 46.0
    assert recent[0].is_estimated is False


def test_save_and_retrieve_weight_log(mem_db: sqlite3.Connection) -> None:
    """Verify persisting weight log and reading it back."""
    log = CalculatedWeightLog(
        raw_input="Váha 84.5 kg",
        weight_kg=84.5,
    )

    saved = save_weight_log(mem_db, log)
    assert saved.id is not None

    recent = get_recent_weight_logs(mem_db, limit=10)
    assert len(recent) == 1
    assert recent[0].weight_kg == 84.5
    assert recent[0].raw_input == "Váha 84.5 kg"


def test_load_foods_from_json_missing_file_fails_fast() -> None:
    """Non-existent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_foods_from_json("non_existent_food_catalog_xyz123.json")


def test_daily_queries_and_latest_weight(mem_db: sqlite3.Connection) -> None:
    """Verify get_daily_food_logs and get_latest_weight_log."""
    from nutrition_agent.repository import get_daily_food_logs, get_latest_weight_log

    today_prefix = "2026-09-17"
    yesterday_prefix = "2026-09-16"

    log_today = CalculatedFoodLog(
        logged_at=f"{today_prefix}T12:00:00Z",
        raw_input="200g kuřecí",
        food_name_matched="Kuřecí prsa",
        amount_g=200.0,
        state="raw",
        is_estimated=False,
        calories=212.0,
        protein_raw=46.0,
        protein_usable=46.0,
        carbs=0.0,
        fats=2.4,
    )
    log_yesterday = CalculatedFoodLog(
        logged_at=f"{yesterday_prefix}T12:00:00Z",
        raw_input="150g rýže",
        food_name_matched="Rýže jasmínová (suchá)",
        amount_g=150.0,
        state="cooked",
        is_estimated=False,
        calories=213.0,
        protein_raw=4.2,
        protein_usable=2.94,
        carbs=47.4,
        fats=0.4,
    )
    save_food_logs(mem_db, [log_today, log_yesterday])

    weight1 = CalculatedWeightLog(
        logged_at=f"{today_prefix}T07:30:00Z",
        raw_input="82.0 kg",
        weight_kg=82.0,
    )
    weight2 = CalculatedWeightLog(
        logged_at=f"{today_prefix}T09:00:00Z",
        raw_input="82.4 kg",
        weight_kg=82.4,
    )
    save_weight_log(mem_db, weight1)
    save_weight_log(mem_db, weight2)

    today_foods = get_daily_food_logs(mem_db, today_prefix)
    assert len(today_foods) == 1
    assert today_foods[0].food_name_matched == "Kuřecí prsa"

    yesterday_foods = get_daily_food_logs(mem_db, yesterday_prefix)
    assert len(yesterday_foods) == 1
    assert yesterday_foods[0].food_name_matched == "Rýže jasmínová (suchá)"

    latest_weight = get_latest_weight_log(mem_db, today_prefix)
    assert latest_weight is not None
    assert latest_weight.weight_kg == 82.4


def test_config_supports_foods_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify config loads FOODS_DB_PATH as alias for foods_data_path."""
    from nutrition_agent.config import Settings

    monkeypatch.setenv("FOODS_DB_PATH", "custom/foods_db.json")
    monkeypatch.delenv("FOODS_DATA_PATH", raising=False)

    s = Settings.load()
    assert s.foods_data_path.as_posix() == "custom/foods_db.json"
    assert s.foods_db_path == s.foods_data_path


def test_redundant_day_types_are_dropped_on_startup(mem_db: sqlite3.Connection) -> None:
    """A stored type that only repeats the workout log is removed.

    Logging a workout used to store the day type as well, so deleting the workout left
    the day announced as a training day with no workout behind it.
    """
    from nutrition_agent.db import _drop_redundant_day_types
    from nutrition_agent.models import CalculatedWorkoutLog
    from nutrition_agent.repository import get_day_type, save_workout_log, set_day_type
    from nutrition_agent.targets import DayType

    save_workout_log(
        mem_db,
        CalculatedWorkoutLog(
            logged_at="2026-09-21T17:00:00+00:00",
            raw_input="trenink gym 60 min",
            workout_type="gym",
            duration_minutes=60,
        ),
    )
    set_day_type(mem_db, "2026-09-21", DayType.TRAINING)  # repeats the workout
    set_day_type(mem_db, "2026-09-20", DayType.REST)  # repeats the empty day
    set_day_type(mem_db, "2026-09-22", DayType.TRAINING)  # a statement of its own

    _drop_redundant_day_types(mem_db)

    rows = [row["date"] for row in mem_db.execute("SELECT date FROM day_types")]
    assert rows == ["2026-09-22"]
    # Nothing changes for the user: every day still resolves to the same type.
    assert get_day_type(mem_db, "2026-09-21") == DayType.TRAINING
    assert get_day_type(mem_db, "2026-09-20") == DayType.REST
    assert get_day_type(mem_db, "2026-09-22") == DayType.TRAINING


def test_day_type_is_stored_per_date(mem_db: sqlite3.Connection) -> None:
    """A day type belongs to one date and never leaks into the next day."""
    from nutrition_agent.repository import get_day_type, set_day_type
    from nutrition_agent.targets import DayType

    set_day_type(mem_db, "2026-09-21", DayType.TRAINING)

    assert get_day_type(mem_db, "2026-09-21") == DayType.TRAINING
    assert get_day_type(mem_db, "2026-09-22") == DayType.REST

    set_day_type(mem_db, "2026-09-21", DayType.REST)
    assert get_day_type(mem_db, "2026-09-21") == DayType.REST

    with pytest.raises(ValueError, match="training"):
        set_day_type(mem_db, "2026-09-21", "svátek")


def test_day_type_falls_back_to_the_logged_workout(mem_db: sqlite3.Connection) -> None:
    """A logged workout makes its own day a training day; an explicit choice wins."""
    from nutrition_agent.models import CalculatedWorkoutLog
    from nutrition_agent.repository import get_day_type, save_workout_log, set_day_type
    from nutrition_agent.targets import DayType

    save_workout_log(
        mem_db,
        CalculatedWorkoutLog(
            logged_at="2026-09-21T17:00:00+00:00",
            raw_input="trenink gym 60 min",
            workout_type="gym",
            duration_minutes=60,
        ),
    )

    assert get_day_type(mem_db, "2026-09-21") == DayType.TRAINING
    assert get_day_type(mem_db, "2026-09-20") == DayType.REST

    # An explicit statement about the same day beats the inference.
    set_day_type(mem_db, "2026-09-21", DayType.REST)
    assert get_day_type(mem_db, "2026-09-21") == DayType.REST


def test_init_db_moves_the_old_global_day_type_onto_its_date() -> None:
    """The old single setting is migrated to the day it was written on.

    Leaving it as one value for all days is what made the day after a workout look like
    a training day.
    """
    from nutrition_agent.db import SCHEMA_SQL, _migrate_stored_day_type
    from tests.scratch import TEST_SCRATCH_ROOT

    # The OS temp directory is not writable in the sandbox, so the file lives in the repo.
    test_dir = TEST_SCRATCH_ROOT
    test_dir.mkdir(parents=True, exist_ok=True)
    db_path = test_dir / "legacy_day_type.db"
    db_path.unlink(missing_ok=True)

    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO user_settings (key, value, updated_at) VALUES ('day_type', ?, ?)",
        ("training", "2026-09-21T17:05:38.373726+00:00"),
    )
    conn.commit()

    _migrate_stored_day_type(conn)
    conn.commit()

    rows = {row["date"]: row["day_type"] for row in conn.execute("SELECT * FROM day_types")}
    assert list(rows) == ["2026-09-21"]
    assert rows["2026-09-21"] == "training"
    remaining = conn.execute("SELECT COUNT(*) FROM user_settings WHERE key = 'day_type'")
    assert remaining.fetchone()[0] == 0

    # Running it again changes nothing and does not raise.
    _migrate_stored_day_type(conn)
    assert conn.execute("SELECT COUNT(*) FROM day_types").fetchone()[0] == 1
    conn.close()
