"""Tests for alcohol as a nutrient and for estimating amounts without a stated weight."""

import sqlite3

import pytest

from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import (
    CalculatedFoodLog,
    EstimatedFoodNutrition,
    ExtractedFoodItem,
    FoodDefinition,
    energy_from_macros,
)
from nutrition_agent.nutrition import build_food_from_estimate, calculate_food_log
from nutrition_agent.repository import (
    ensure_food_catalog,
    get_food,
    get_recent_food_logs,
    load_foods,
    save_food_logs,
    save_learned_food,
)

CATALOG_JSON = "data/foods.json"


@pytest.fixture
def db() -> sqlite3.Connection:
    """Seeded catalog database."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    ensure_food_catalog(conn, CATALOG_JSON)
    return conn


def _beer() -> EstimatedFoodNutrition:
    return EstimatedFoodNutrition(
        name="ležák 12°",
        calories=43.0,
        protein=0.5,
        carbs=3.5,
        fats=0.0,
        alcohol=4.0,
        protein_category="grains_seeds",
    )


def test_energy_from_macros_counts_ethanol():
    """Ethanol carries 7 kcal/g and must be part of the energy check."""
    assert energy_from_macros(10.0, 10.0, 10.0, 0.0) == 170.0
    assert energy_from_macros(0.0, 0.0, 0.0, 10.0) == 70.0


def test_beer_estimate_is_accepted():
    """Beer used to be rejected because its calories come from alcohol, not macros."""
    beer = _beer()
    assert beer.alcohol == 4.0

    macro_energy = energy_from_macros(beer.protein, beer.carbs, beer.fats, beer.alcohol)
    deviation = abs(macro_energy - beer.calories) / beer.calories
    assert deviation < 0.35


def test_spirit_estimate_is_accepted():
    """A spirit is almost pure ethanol."""
    rum = EstimatedFoodNutrition(
        name="rum 40%",
        calories=224.0,
        protein=0.0,
        carbs=0.0,
        fats=0.0,
        alcohol=32.0,
    )
    assert energy_from_macros(rum.protein, rum.carbs, rum.fats, rum.alcohol) == 224.0


def test_estimate_still_rejects_impossible_energy():
    """Adding alcohol must not weaken the consistency guard."""
    with pytest.raises(ValueError, match="nekonzistentní"):
        EstimatedFoodNutrition(
            name="nesmysl",
            calories=100.0,
            protein=1.0,
            carbs=1.0,
            fats=50.0,
            alcohol=0.0,
        )


def test_estimate_rejects_negative_alcohol():
    """A negative alcohol value fails fast."""
    with pytest.raises(ValueError, match="nesmí být záporná"):
        EstimatedFoodNutrition(
            name="pivo", calories=43.0, protein=0.5, carbs=3.5, fats=0.0, alcohol=-1.0
        )


def test_build_food_from_estimate_carries_alcohol():
    """The learned catalog entry keeps the alcohol content."""
    food = build_food_from_estimate(_beer())
    assert food.alcohol == 4.0
    assert food.source == "ai"


def test_alcohol_survives_the_catalog_round_trip(db: sqlite3.Connection):
    """Alcohol is stored and read back from the foods table."""
    save_learned_food(db, build_food_from_estimate(_beer()))

    stored = get_food(db, "ležák 12°")
    assert stored is not None
    assert stored.alcohol == 4.0
    assert stored.calories == 43.0

    assert any(food.alcohol > 0 for food in load_foods(db))


def test_catalog_without_alcohol_defaults_to_zero(db: sqlite3.Connection):
    """Curated foods carry no alcohol unless stated."""
    chicken = get_food(db, "kuřecí prsa")
    assert chicken is not None
    assert chicken.alcohol == 0.0


def test_calculate_food_log_scales_alcohol():
    """Alcohol is scaled with the portion like any other nutrient."""
    beer = build_food_from_estimate(_beer())
    item = ExtractedFoodItem(name="pivo", amount=500.0, unit="ml", state="raw")

    log = calculate_food_log(item, beer, "500ml piva", amount_g=500.0)

    assert log.alcohol == 20.0
    assert log.calories == 215.0
    # The stored energy matches what the macros plus ethanol explain
    explained = energy_from_macros(log.protein_raw, log.carbs, log.fats, log.alcohol)
    assert abs(explained - log.calories) / log.calories < 0.1


def test_alcohol_survives_the_log_round_trip(db: sqlite3.Connection):
    """A logged beer keeps its alcohol in the database."""
    beer = build_food_from_estimate(_beer())
    item = ExtractedFoodItem(name="pivo", amount=500.0, unit="ml", state="raw")

    save_food_logs(db, [calculate_food_log(item, beer, "500ml piva", amount_g=500.0)])

    stored = get_recent_food_logs(db)[0]
    assert stored.alcohol == 20.0
    assert stored.calories == 215.0


def test_log_row_accepts_alcohol_directly(db: sqlite3.Connection):
    """A hand-built log with alcohol persists without loss."""
    save_food_logs(
        db,
        [
            CalculatedFoodLog(
                raw_input="panák rumu",
                food_name_matched="rum 40%",
                amount_g=40.0,
                state="raw",
                is_estimated=True,
                calories=90.0,
                protein_raw=0.0,
                protein_usable=0.0,
                carbs=0.0,
                fats=0.0,
                alcohol=12.8,
            )
        ],
    )

    stored = get_recent_food_logs(db)[0]
    assert stored.alcohol == 12.8


def test_legacy_database_gains_alcohol_columns():
    """Databases created before this feature are migrated in place."""
    from nutrition_agent.db import _apply_column_migrations, _existing_columns

    conn = get_connection(":memory:")
    conn.executescript(
        """
        CREATE TABLE food_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            raw_input TEXT NOT NULL,
            food_name_matched TEXT NOT NULL,
            amount_g REAL NOT NULL,
            state TEXT NOT NULL,
            is_estimated INTEGER NOT NULL,
            calories REAL NOT NULL,
            protein_raw REAL NOT NULL,
            protein_usable REAL NOT NULL,
            carbs REAL NOT NULL,
            fats REAL NOT NULL
        );
        CREATE TABLE foods (
            name TEXT PRIMARY KEY,
            name_key TEXT NOT NULL UNIQUE,
            calories REAL NOT NULL,
            protein REAL NOT NULL,
            diaas_coefficient REAL NOT NULL,
            carbs REAL NOT NULL,
            fats REAL NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    assert "alcohol" not in _existing_columns(conn, "food_logs")
    assert "alcohol" not in _existing_columns(conn, "foods")

    _apply_column_migrations(conn)

    assert "alcohol" in _existing_columns(conn, "food_logs")
    assert "alcohol" in _existing_columns(conn, "foods")


def test_food_definition_defaults_alcohol_to_zero():
    """Existing call sites keep working without passing alcohol."""
    food = FoodDefinition(name="x", calories=100.0, protein=1.0, carbs=1.0, fats=1.0)
    assert food.alcohol == 0.0
