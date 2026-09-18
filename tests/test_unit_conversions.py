"""Tests for household unit conversion, learning and catalog management."""

import sqlite3

import pytest

from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import EstimatedUnitConversion, FoodDefinition
from nutrition_agent.nutrition import convert_to_grams, lookup_unit_weight
from nutrition_agent.repository import (
    ensure_food_catalog,
    get_food,
    get_unit_conversion,
    list_unit_conversions,
    load_foods,
    save_learned_food,
    save_unit_conversion,
)

CATALOG_JSON = "data/foods.json"


@pytest.fixture
def db() -> sqlite3.Connection:
    """Seeded catalog database."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    ensure_food_catalog(conn, CATALOG_JSON)
    return conn


def test_absolute_units_pass_through():
    """Grams and millilitres carry their own amount."""
    assert convert_to_grams("cokoliv", 150.0, "g") == 150.0
    assert convert_to_grams("cokoliv", 200.0, "ml") == 200.0
    assert convert_to_grams("cokoliv", 150.0, "G") == 150.0


def test_generic_measures_use_the_curated_table():
    """Household measures have fixed weights independent of the food."""
    assert convert_to_grams("olivový olej", 1.0, "lžíce") == 13.0
    assert convert_to_grams("olivový olej", 1.0, "lžička") == 5.0
    assert convert_to_grams("rýže", 1.0, "hrnek") == 240.0
    assert convert_to_grams("protein", 2.0, "odměrka") == 60.0
    assert convert_to_grams("šunka", 2.0, "plátek") == 40.0
    assert convert_to_grams("chléb", 1.0, "krajíc") == 40.0


def test_piece_weights_are_food_specific():
    """A piece uses the weight tied to that food."""
    assert convert_to_grams("Vejce (celé)", 2.0, "ks") == 110.0
    assert convert_to_grams("Banán", 1.0, "ks") == 120.0
    assert convert_to_grams("Jablko", 1.0, "ks") == 150.0


def test_unknown_conversion_returns_none_instead_of_guessing():
    """Anything not in the tables reports 'unknown' rather than inventing a weight.

    The previous implementation silently used 100 g per piece and 150 g per portion,
    which produced numbers that looked measured but were made up.
    """
    assert convert_to_grams("Šunka vepřová výběrová", 1.0, "ks") is None
    assert convert_to_grams("svíčková", 1.0, "porce") is None
    assert convert_to_grams("čokoláda", 1.0, "tabulka") is None


def test_lookup_unit_weight_prefers_food_specific_piece():
    """Piece weights win over the generic table for countable foods."""
    assert lookup_unit_weight("Vejce (celé)", "ks") == 55.0
    assert lookup_unit_weight("neznámá věc", "ks") is None
    assert lookup_unit_weight("cokoliv", "lžíce") == 13.0


def test_conversion_rejects_non_positive_amount():
    """A non-positive amount fails fast."""
    with pytest.raises(ValueError, match="kladné číslo"):
        convert_to_grams("rýže", 0.0, "g")


def test_estimated_unit_conversion_validation():
    """The estimate schema rejects impossible weights but allows 'unknown'."""
    valid = EstimatedUnitConversion(name="protein", unit="odměrka", grams_per_unit=30.0)
    assert valid.grams_per_unit == 30.0

    # Zero is the documented way for the model to say it does not know
    unknown = EstimatedUnitConversion(name="x", unit="tabulka", grams_per_unit=0.0)
    assert unknown.grams_per_unit == 0.0

    with pytest.raises(ValueError, match="nesmí být záporná"):
        EstimatedUnitConversion(name="x", unit="odměrka", grams_per_unit=-5.0)

    with pytest.raises(ValueError, match="mimo rozumný rozsah"):
        EstimatedUnitConversion(name="x", unit="porce", grams_per_unit=9000.0)


def test_unit_conversion_roundtrip(db: sqlite3.Connection):
    """Stored conversions can be read back with their source."""
    assert get_unit_conversion(db, "svíčková", "porce") is None

    save_unit_conversion(db, "svíčková", "porce", 350.0, source="ai")
    stored = get_unit_conversion(db, "svíčková", "porce")
    assert stored == (350.0, "ai")

    # Saving again overwrites and can promote the source to 'user'
    save_unit_conversion(db, "svíčková", "porce", 320.0, source="user")
    assert get_unit_conversion(db, "svíčková", "porce") == (320.0, "user")


def test_unit_conversion_lookup_is_case_insensitive(db: sqlite3.Connection):
    """Conversions are keyed by the normalized food name."""
    save_unit_conversion(db, "Svíčková na smetaně", "PORCE", 350.0)
    assert get_unit_conversion(db, "svíčková na smetaně", "porce") == (350.0, "ai")


def test_unit_conversion_rejects_unknown_source(db: sqlite3.Connection):
    """Only user corrections and AI estimates may be stored."""
    with pytest.raises(ValueError, match="'user' nebo 'ai'"):
        save_unit_conversion(db, "x", "porce", 100.0, source="guess")


def test_list_unit_conversions(db: sqlite3.Connection):
    """All learned conversions can be listed."""
    assert list_unit_conversions(db) == []

    save_unit_conversion(db, "svíčková", "porce", 350.0)
    save_unit_conversion(db, "protein", "odměrka", 30.0)

    listed = list_unit_conversions(db)
    assert len(listed) == 2
    assert {item["food_key"] for item in listed} == {"svíčková", "protein"}


def test_delete_food_removes_food_and_its_conversions(db: sqlite3.Connection):
    """Deleting a catalog food also drops conversions tied to it."""
    save_learned_food(
        db,
        FoodDefinition(
            name="marmeláda",
            calories=224.0,
            protein=0.4,
            carbs=59.0,
            fats=0.1,
            source="ai",
        ),
    )
    save_unit_conversion(db, "marmeláda", "lžíce", 20.0)

    from nutrition_agent.repository import delete_food

    removed = delete_food(db, "MARMELÁDA")
    assert removed.name == "marmeláda"
    assert get_food(db, "marmeláda") is None
    assert get_unit_conversion(db, "marmeláda", "lžíce") is None


def test_delete_unknown_food_fails_fast(db: sqlite3.Connection):
    """Deleting a food that does not exist raises LookupError."""
    from nutrition_agent.repository import delete_food

    with pytest.raises(LookupError, match="není v katalogu"):
        delete_food(db, "neexistuje")


def test_list_foods_by_source(db: sqlite3.Connection):
    """The catalog can be split into curated and AI-learned layers."""
    from nutrition_agent.repository import list_foods_by_source

    save_learned_food(
        db,
        FoodDefinition(
            name="marmeláda",
            calories=224.0,
            protein=0.4,
            carbs=59.0,
            fats=0.1,
            source="ai",
        ),
    )

    assert [f.name for f in list_foods_by_source(db, "ai")] == ["marmeláda"]
    assert all(f.source == "curated" for f in list_foods_by_source(db, "curated"))
    assert len(list_foods_by_source(db, "curated")) == len(load_foods(db)) - 1


def test_food_log_persists_conversion_flag(db: sqlite3.Connection):
    """The estimated-conversion flag survives the database round trip."""
    from nutrition_agent.models import CalculatedFoodLog
    from nutrition_agent.repository import get_recent_food_logs, save_food_logs

    save_food_logs(
        db,
        [
            CalculatedFoodLog(
                raw_input="2 porce svíčkové",
                food_name_matched="svíčková",
                amount_g=700.0,
                state="cooked",
                is_estimated=True,
                calories=1000.0,
                protein_raw=60.0,
                protein_usable=60.0,
                carbs=50.0,
                fats=60.0,
                nutrition_source="ai",
                conversion_estimated=True,
            )
        ],
    )

    stored = get_recent_food_logs(db)[0]
    assert stored.conversion_estimated is True
    assert stored.nutrition_source == "ai"


def test_legacy_database_gains_conversion_column():
    """A database created before this feature is migrated in place."""
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
        """
    )
    assert "conversion_estimated" not in _existing_columns(conn, "food_logs")

    _apply_column_migrations(conn)

    columns = _existing_columns(conn, "food_logs")
    assert "conversion_estimated" in columns
    assert "nutrition_source" in columns
