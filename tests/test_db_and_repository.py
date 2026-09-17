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
