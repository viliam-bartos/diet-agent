"""Tests for the food catalog, AI-learned foods and manual corrections."""

import sqlite3

import pytest

from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import EstimatedFoodNutrition, FoodDefinition
from nutrition_agent.nutrition import build_food_from_estimate
from nutrition_agent.repository import (
    ensure_food_catalog,
    get_food,
    load_foods,
    load_foods_from_json,
    save_learned_food,
    update_food_nutrition,
)

CATALOG_JSON = "data/foods.json"


@pytest.fixture
def catalog_db() -> sqlite3.Connection:
    """Empty database with the full schema applied."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    return conn


def test_ensure_food_catalog_seeds_curated_foods(catalog_db: sqlite3.Connection):
    """Seeding copies the curated JSON catalog into the foods table."""
    expected = load_foods_from_json(CATALOG_JSON)
    unique_names = {food.name.strip().lower() for food in expected}
    inserted = ensure_food_catalog(catalog_db, CATALOG_JSON)

    # The JSON file may repeat a food; the catalog stores one row per name.
    assert inserted == len(unique_names)
    stored = load_foods(catalog_db)
    assert len(stored) == len(unique_names)
    assert all(food.source == "curated" for food in stored)
    assert {food.name.lower() for food in stored} == unique_names


def test_ensure_food_catalog_is_idempotent(catalog_db: sqlite3.Connection):
    """Running the seed twice inserts nothing the second time."""
    first = ensure_food_catalog(catalog_db, CATALOG_JSON)
    second = ensure_food_catalog(catalog_db, CATALOG_JSON)

    assert first > 0
    assert second == 0


def test_ensure_food_catalog_never_overwrites_corrected_values(
    catalog_db: sqlite3.Connection,
):
    """A user correction survives a repeated catalog seed."""
    ensure_food_catalog(catalog_db, CATALOG_JSON)
    update_food_nutrition(catalog_db, "Kuřecí prsa", 999.0, 1.0, 2.0, 3.0)

    ensure_food_catalog(catalog_db, CATALOG_JSON)

    stored = get_food(catalog_db, "kuřecí prsa")
    assert stored is not None
    assert stored.calories == 999.0


def test_load_foods_puts_curated_first(catalog_db: sqlite3.Connection):
    """Curated entries sort before AI entries so fuzzy matching prefers them."""
    ensure_food_catalog(catalog_db, CATALOG_JSON)
    save_learned_food(
        catalog_db,
        FoodDefinition(
            name="aaa vymyslene jidlo",
            calories=100.0,
            protein=1.0,
            carbs=1.0,
            fats=1.0,
            source="ai",
        ),
    )

    foods = load_foods(catalog_db)
    sources = [food.source for food in foods]
    assert sources == sorted(sources, key=lambda s: 0 if s == "curated" else 1)
    assert foods[-1].source == "ai"


def test_load_foods_can_filter_by_source(catalog_db: sqlite3.Connection):
    """Filtering by source returns only AI-learned entries."""
    ensure_food_catalog(catalog_db, CATALOG_JSON)
    save_learned_food(
        catalog_db,
        FoodDefinition(
            name="quinoa",
            calories=368.0,
            protein=14.0,
            carbs=64.0,
            fats=6.0,
            source="ai",
        ),
    )

    ai_foods = load_foods(catalog_db, source="ai")
    assert [food.name for food in ai_foods] == ["quinoa"]


def test_save_learned_food_reports_new_and_existing(catalog_db: sqlite3.Connection):
    """The first save inserts, a repeated save of the same name does not."""
    food = FoodDefinition(
        name="quinoa",
        calories=368.0,
        protein=14.0,
        carbs=64.0,
        fats=6.0,
        source="ai",
    )
    assert save_learned_food(catalog_db, food) is True
    assert save_learned_food(catalog_db, food) is False


def test_save_learned_food_rejects_curated_source(catalog_db: sqlite3.Connection):
    """Only AI estimates may be written through the learned-food path."""
    curated = FoodDefinition(
        name="neco",
        calories=100.0,
        protein=1.0,
        carbs=1.0,
        fats=1.0,
        source="curated",
    )
    with pytest.raises(ValueError, match="source='ai'"):
        save_learned_food(catalog_db, curated)


def test_get_food_is_case_insensitive(catalog_db: sqlite3.Connection):
    """Lookups ignore letter case, including Czech diacritics."""
    ensure_food_catalog(catalog_db, CATALOG_JSON)

    assert get_food(catalog_db, "KUŘECÍ PRSA") is not None
    assert get_food(catalog_db, "kuřecí prsa") is not None
    assert get_food(catalog_db, "  Kuřecí   Prsa  ") is not None
    assert get_food(catalog_db, "neexistuje") is None


def test_food_name_key_folds_czech_diacritics(catalog_db: sqlite3.Connection):
    """The lookup key is Unicode-aware, unlike SQLite's NOCASE collation.

    SQLite's built-in NOCASE only folds ASCII, so 'KUŘECÍ' would not match
    'kuřecí' without the precomputed key.
    """
    from nutrition_agent.repository import food_name_key

    assert food_name_key("Kuřecí Prsa") == food_name_key("KUŘECÍ PRSA")
    assert food_name_key("  Čočka   hnědá ") == "čočka hnědá"
    assert food_name_key("Žluťoučký") == food_name_key("žluťoučký")


def test_update_food_nutrition_overwrites_values(catalog_db: sqlite3.Connection):
    """A manual correction replaces calories and macros."""
    ensure_food_catalog(catalog_db, CATALOG_JSON)
    updated = update_food_nutrition(catalog_db, "rýže jasmínová (suchá)", 360.0, 7.5, 80.0, 0.8)

    assert updated.name == "Rýže jasmínová (suchá)"
    assert updated.calories == 360.0
    assert updated.protein == 7.5
    assert updated.carbs == 80.0
    assert updated.fats == 0.8
    # The DIAAS coefficient is a category property and stays untouched
    assert updated.diaas_coefficient == 0.70


def test_update_food_nutrition_unknown_food_fails_fast(catalog_db: sqlite3.Connection):
    """Correcting a food that is not in the catalog raises LookupError."""
    with pytest.raises(LookupError, match="není v katalogu"):
        update_food_nutrition(catalog_db, "neexistuje", 1.0, 1.0, 1.0, 1.0)


def test_build_food_from_estimate_maps_category_to_diaas():
    """The protein category decides the DIAAS coefficient."""
    animal = build_food_from_estimate(
        EstimatedFoodNutrition(
            name="Kuřecí Kari",
            calories=130.0,
            protein=12.0,
            carbs=6.0,
            fats=6.0,
            protein_category="animal",
        )
    )
    assert animal.source == "ai"
    assert animal.name == "kuřecí kari"
    assert animal.diaas_coefficient == 1.00

    legume = build_food_from_estimate(
        EstimatedFoodNutrition(
            name="čočka beluga",
            calories=350.0,
            protein=25.0,
            carbs=60.0,
            fats=1.0,
            protein_category="legumes",
        )
    )
    assert legume.diaas_coefficient == 0.75


def test_build_food_from_estimate_unknown_category_is_conservative():
    """An unsure category falls back to the conservative default coefficient."""
    food = build_food_from_estimate(
        EstimatedFoodNutrition(
            name="záhadná směs",
            calories=200.0,
            protein=10.0,
            carbs=20.0,
            fats=8.0,
            protein_category="unknown",
        )
    )
    assert food.diaas_coefficient == 0.70


def test_build_food_from_estimate_rejects_empty_estimate():
    """An estimate with no usable values is rejected instead of stored."""
    with pytest.raises(ValueError, match="nezná potravinu"):
        build_food_from_estimate(
            EstimatedFoodNutrition(
                name="nesmysl",
                calories=0.0,
                protein=0.0,
                carbs=0.0,
                fats=0.0,
            )
        )


def test_estimate_rejects_negative_values():
    """Negative nutrition values fail fast."""
    with pytest.raises(ValueError, match="nesmí být záporná"):
        EstimatedFoodNutrition(
            name="test",
            calories=-1.0,
            protein=1.0,
            carbs=1.0,
            fats=1.0,
        )


def test_estimate_rejects_impossible_energy():
    """Macros that cannot match the stated energy are rejected."""
    with pytest.raises(ValueError, match="nekonzistentní"):
        EstimatedFoodNutrition(
            name="test",
            calories=100.0,
            protein=1.0,
            carbs=1.0,
            fats=50.0,  # 50 g fat alone is ~450 kcal
        )


def test_estimate_accepts_consistent_macros():
    """A plausible estimate passes validation."""
    estimate = EstimatedFoodNutrition(
        name="test",
        calories=200.0,
        protein=20.0,
        carbs=20.0,
        fats=4.0,  # 80 + 80 + 36 = 196 kcal
        protein_category="animal",
    )
    assert estimate.calories == 200.0

    # Zero-calorie entries are allowed (model did not know the food)
    empty = EstimatedFoodNutrition(name="nezname", calories=0.0, protein=0.0, carbs=0.0, fats=0.0)
    assert empty.protein_category == "unknown"


@pytest.fixture
def cli_db() -> sqlite3.Connection:
    """Seeded catalog database for command-level tests."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    ensure_food_catalog(conn, CATALOG_JSON)
    return conn


def test_jidlo_command_shows_current_values(cli_db: sqlite3.Connection):
    """'jidlo <nazev>' inspects a food without changing it."""
    from nutrition_agent.cli import correct_food_nutrition

    result = correct_food_nutrition(cli_db, "kuřecí prsa")
    assert "Kuřecí prsa" in result
    assert "kurátorovaný katalog" in result
    assert "106.0 kcal" in result
    assert "23.00 g" in result
    assert "DIAAS koeficient: 1.00" in result

    unchanged = get_food(cli_db, "kuřecí prsa")
    assert unchanged is not None and unchanged.calories == 106.0


def test_jidlo_command_corrects_values(cli_db: sqlite3.Connection):
    """'jidlo <nazev> <kcal> <B> <S> <T>' overwrites the stored values."""
    from nutrition_agent.cli import correct_food_nutrition

    result = correct_food_nutrition(cli_db, "kuřecí prsa 120 24 0 1.5")
    assert result.startswith("Opraveno: Kuřecí prsa")
    assert "120.0 kcal" in result
    assert "Už zaznamenané dny se nepřepočítávají" in result

    stored = get_food(cli_db, "kuřecí prsa")
    assert stored is not None
    assert (stored.calories, stored.protein, stored.carbs, stored.fats) == (120.0, 24.0, 0.0, 1.5)


def test_jidlo_command_handles_multiword_names(cli_db: sqlite3.Connection):
    """Names containing spaces are separated from trailing numeric values."""
    from nutrition_agent.cli import correct_food_nutrition

    result = correct_food_nutrition(cli_db, "rýže jasmínová (suchá) 360 7.5 80 0.8")
    assert "Rýže jasmínová (suchá)" in result

    stored = get_food(cli_db, "rýže jasmínová (suchá)")
    assert stored is not None and stored.calories == 360.0


def test_jidlo_command_accepts_decimal_comma(cli_db: sqlite3.Connection):
    """Czech decimal commas are accepted in the corrected values."""
    from nutrition_agent.cli import correct_food_nutrition

    correct_food_nutrition(cli_db, "kuřecí prsa 120,5 24,5 0,5 1,5")
    stored = get_food(cli_db, "kuřecí prsa")
    assert stored is not None
    assert stored.calories == 120.5
    assert stored.protein == 24.5


def test_jidlo_command_unknown_food_explains_itself(cli_db: sqlite3.Connection):
    """Correcting a food that was never logged gives a helpful message."""
    from nutrition_agent.cli import correct_food_nutrition

    result = correct_food_nutrition(cli_db, "neexistuje 100 1 1 1")
    assert "není v katalogu" in result


def test_jidlo_command_partial_numbers_are_rejected(cli_db: sqlite3.Connection):
    """Supplying fewer than four values does not silently change anything."""
    from nutrition_agent.cli import correct_food_nutrition

    result = correct_food_nutrition(cli_db, "kuřecí prsa 120 24")
    assert "zadej všechny čtyři hodnoty" in result

    stored = get_food(cli_db, "kuřecí prsa")
    assert stored is not None and stored.calories == 106.0


def test_jidlo_command_rejects_negative_values(cli_db: sqlite3.Connection):
    """Negative corrections are refused."""
    from nutrition_agent.cli import correct_food_nutrition

    result = correct_food_nutrition(cli_db, "kuřecí prsa -5 24 0 1")
    assert "nesmí být záporné" in result

    stored = get_food(cli_db, "kuřecí prsa")
    assert stored is not None and stored.calories == 106.0


def test_jidlo_command_requires_a_name(cli_db: sqlite3.Connection):
    """An empty argument list returns usage instead of raising."""
    from nutrition_agent.cli import correct_food_nutrition

    assert "Použití" in correct_food_nutrition(cli_db, "")
    assert "Chybí název" in correct_food_nutrition(cli_db, "  100 1 1 1")


def test_jidlo_command_via_message_handler(cli_db: sqlite3.Connection):
    """The 'jidlo' keyword routes through handle_user_message."""
    from nutrition_agent.cli import handle_user_message

    assert "Kuřecí prsa" in handle_user_message("jidlo kuřecí prsa", [], cli_db)
    assert "Použití" in handle_user_message("jidlo", [], cli_db)
    assert "Opraveno" in handle_user_message("jidlo kuřecí prsa 110 23 0 1.2", [], cli_db)


def test_corrected_ai_food_stays_marked_as_ai(cli_db: sqlite3.Connection):
    """Correcting an AI food keeps its source flag but updates the numbers."""
    from nutrition_agent.cli import correct_food_nutrition

    save_learned_food(
        cli_db,
        FoodDefinition(
            name="kuřecí kari",
            calories=130.0,
            protein=12.0,
            carbs=6.0,
            fats=6.0,
            source="ai",
        ),
    )
    correct_food_nutrition(cli_db, "kuřecí kari 145 13 7 7")

    stored = get_food(cli_db, "kuřecí kari")
    assert stored is not None
    assert stored.source == "ai"
    assert stored.calories == 145.0
