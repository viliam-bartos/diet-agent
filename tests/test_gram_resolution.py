"""Tests for the CLI gram resolution chain and catalog management commands."""

import sqlite3

import pytest

import nutrition_agent.cli as cli_module
from nutrition_agent.cli import (
    correct_food_nutrition,
    delete_catalog_food,
    handle_user_message,
    list_catalog_entries,
    resolve_grams,
    show_unit_conversions,
)
from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import EstimatedUnitConversion, FoodDefinition, UserLog
from nutrition_agent.repository import (
    ensure_food_catalog,
    get_food,
    get_unit_conversion,
    load_foods,
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


def _food(name: str, source: str = "curated") -> FoodDefinition:
    return FoodDefinition(
        name=name, calories=100.0, protein=10.0, carbs=10.0, fats=1.0, source=source
    )


def test_resolve_grams_uses_curated_table(db: sqlite3.Connection):
    """Known measures never reach the model."""
    grams, estimated = resolve_grams(db, _food("Olivový olej"), 1.0, "lžíce")
    assert grams == 13.0
    assert estimated is False


def test_resolve_grams_uses_learned_conversion(db: sqlite3.Connection):
    """A stored conversion is reused without asking the model."""
    cli_module.save_unit_conversion(db, "svíčková", "porce", 350.0, source="ai")

    grams, estimated = resolve_grams(db, _food("svíčková", source="ai"), 2.0, "porce")
    assert grams == 700.0
    assert estimated is True


def test_resolve_grams_asks_model_and_caches(db: sqlite3.Connection, monkeypatch):
    """An unknown measure is estimated once and then stored for reuse."""
    calls: list[tuple[str, str]] = []

    def fake_estimate(food_name: str, unit: str, **kwargs) -> EstimatedUnitConversion:
        calls.append((food_name, unit))
        return EstimatedUnitConversion(name=food_name, unit=unit, grams_per_unit=350.0)

    monkeypatch.setattr(cli_module, "estimate_unit_conversion", fake_estimate)

    grams, estimated = resolve_grams(db, _food("svíčková"), 1.0, "porce")
    assert grams == 350.0
    assert estimated is True
    assert calls == [("svíčková", "porce")]

    # Second call for the same pair must come from the cache
    grams2, estimated2 = resolve_grams(db, _food("svíčková"), 2.0, "porce")
    assert grams2 == 700.0
    assert calls == [("svíčková", "porce")]
    assert get_unit_conversion(db, "svíčková", "porce") == (350.0, "ai")


def test_resolve_grams_refuses_when_model_does_not_know(db: sqlite3.Connection, monkeypatch):
    """A zero estimate is refused instead of storing an invented weight."""
    monkeypatch.setattr(
        cli_module,
        "estimate_unit_conversion",
        lambda food_name, unit, **kwargs: EstimatedUnitConversion(
            name=food_name, unit=unit, grams_per_unit=0.0
        ),
    )

    with pytest.raises(ValueError, match="Nevím, kolik gramů"):
        resolve_grams(db, _food("zvláštní věc"), 1.0, "tabulka")

    assert get_unit_conversion(db, "zvláštní věc", "tabulka") is None


def test_resolve_grams_refuses_when_model_fails(db: sqlite3.Connection, monkeypatch):
    """A broken model response becomes a clear message, not a traceback."""

    def broken(food_name: str, unit: str, **kwargs):
        raise RuntimeError("Model nevrátil převod.")

    monkeypatch.setattr(cli_module, "estimate_unit_conversion", broken)

    with pytest.raises(ValueError, match="Nevím, kolik gramů"):
        resolve_grams(db, _food("zvláštní věc"), 1.0, "tabulka")

    assert get_unit_conversion(db, "zvláštní věc", "tabulka") is None


def test_handle_meal_with_household_measure(db: sqlite3.Connection):
    """'2 odměrky proteinu' resolves through the curated table end to end."""
    conn = db

    def mock_parser(msg: str) -> UserLog:
        from nutrition_agent.models import ExtractedFoodItem

        return UserLog(
            log_type="FOOD",
            items=[
                ExtractedFoodItem(
                    name="syrovátkový protein (80%)",
                    amount=2.0,
                    unit="odměrka",
                    state="raw",
                )
            ],
        )

    result = handle_user_message("2 odměrky proteinu", load_foods(conn), conn, parse_fn=mock_parser)

    assert "60 g" in result
    assert "odhad převodu" not in result

    cursor = conn.cursor()
    cursor.execute("SELECT amount_g, conversion_estimated FROM food_logs")
    row = cursor.fetchone()
    assert row["amount_g"] == 60.0
    assert row["conversion_estimated"] == 0


def test_handle_meal_with_estimated_conversion_is_flagged(db: sqlite3.Connection, monkeypatch):
    """An estimated unit weight is stored, flagged and reported."""
    conn = db
    monkeypatch.setattr(
        cli_module,
        "estimate_unit_conversion",
        lambda food_name, unit, **kwargs: EstimatedUnitConversion(
            name=food_name, unit=unit, grams_per_unit=350.0
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "estimate_food_nutrition",
        lambda food_name, **kwargs: pytest.fail("potravina je v katalogu, nemá se odhadovat"),
    )

    from nutrition_agent.models import ExtractedFoodItem

    def mock_parser(msg: str) -> UserLog:
        return UserLog(
            log_type="FOOD",
            items=[ExtractedFoodItem(name="svíčková", amount=1.0, unit="porce", state="cooked")],
        )

    result = handle_message_with_extra_food(conn, mock_parser)
    assert "odhad převodu" in result
    assert "1 porce = 350 g" in result
    assert "Oprava: prevod svíčková porce <gramy>" in result


def handle_message_with_extra_food(conn: sqlite3.Connection, parse_fn) -> str:
    """Log a meal for a food the helper pre-registers in the catalog."""
    save_learned_food(
        conn,
        FoodDefinition(
            name="svíčková",
            calories=162.0,
            protein=9.0,
            carbs=9.0,
            fats=10.0,
            source="ai",
        ),
    )
    return handle_user_message("svíčková", load_foods(conn), conn, parse_fn=parse_fn)


def test_list_catalog_entries_filters_by_layer(db: sqlite3.Connection):
    """'jidla ai' lists only learned foods, plain 'jidla' lists everything."""
    save_learned_food(
        db,
        FoodDefinition(
            name="marmeláda", calories=224.0, protein=0.4, carbs=59.0, fats=0.1, source="ai"
        ),
    )

    ai_list = list_catalog_entries(db, "ai")
    assert "marmeláda" in ai_list
    assert "Kuřecí prsa" not in ai_list

    everything = list_catalog_entries(db, "")
    assert "marmeláda" in everything
    assert "Kuřecí prsa" in everything

    curated_only = list_catalog_entries(db, "curated")
    assert "Kuřecí prsa" in curated_only
    assert "marmeláda" not in curated_only


def test_list_catalog_entries_rejects_unknown_selector(db: sqlite3.Connection):
    """An unknown filter returns usage instead of dumping the catalog."""
    assert "Použití" in list_catalog_entries(db, "nesmysl")


def test_delete_catalog_food_reports_result(db: sqlite3.Connection):
    """Deleting an AI food reports what was removed."""
    save_learned_food(
        db,
        FoodDefinition(
            name="marmeláda", calories=224.0, protein=0.4, carbs=59.0, fats=0.1, source="ai"
        ),
    )

    result = delete_catalog_food(db, "marmeláda")
    assert "Smazáno z katalogu: marmeláda" in result
    assert get_food(db, "marmeláda") is None

    assert "není v katalogu" in delete_catalog_food(db, "marmeláda")


def test_delete_catalog_food_warns_about_curated(db: sqlite3.Connection):
    """Deleting a curated food is allowed but explicitly called out."""
    result = delete_catalog_food(db, "Kuřecí prsa")
    assert "kurátorovanou" in result


def test_show_and_correct_unit_conversions(db: sqlite3.Connection):
    """'prevod' lists, and 'prevod <nazev> <jednotka> <gramy>' overwrites."""
    assert "Žádné naučené převody" in show_unit_conversions(db, "")

    saved = show_unit_conversions(db, "svíčková porce 350")
    assert "1 porce = 350.0 g (uživatel)" in saved
    assert get_unit_conversion(db, "svíčková", "porce") == (350.0, "user")

    listed = show_unit_conversions(db, "")
    assert "svíčková | 1 porce = 350.0 g (uživatel)" in listed


def test_show_unit_conversions_handles_bad_input(db: sqlite3.Connection):
    """Malformed corrections return usage instead of raising."""
    assert "Použití" in show_unit_conversions(db, "svíčková porce")
    assert "není číslo" in show_unit_conversions(db, "svíčková porce abc")


def test_message_handler_routes_management_commands(db: sqlite3.Connection):
    """The new keywords reach their handlers through handle_user_message."""
    save_learned_food(
        db,
        FoodDefinition(
            name="marmeláda", calories=224.0, protein=0.4, carbs=59.0, fats=0.1, source="ai"
        ),
    )
    foods = load_foods(db)

    assert "marmeláda" in handle_user_message("jidla ai", foods, db)
    assert "Katalog potravin" in handle_user_message("jidla", foods, db)
    assert "Smazáno z katalogu" in handle_user_message("jidla smaz marmeláda", foods, db)
    assert get_food(db, "marmeláda") is None
    assert "Převod uložen" in handle_user_message("prevod svíčková porce 350", foods, db)
    assert "Naučené převody" in handle_user_message("prevod", foods, db)


def test_correct_food_still_works_after_unit_changes(db: sqlite3.Connection):
    """Regression: nutrition correction is unaffected by the unit work."""
    result = correct_food_nutrition(db, "kuřecí prsa 120 24 0 1.5")
    assert result.startswith("Opraveno: Kuřecí prsa")
    stored = get_food(db, "kuřecí prsa")
    assert stored is not None and stored.calories == 120.0
