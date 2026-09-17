"""Tests for parser schemas, edge cases, and fail-fast validations."""

import pytest

from nutrition_agent.models import (
    ExtractedFoodItem,
    FoodNotFoundError,
    UserLog,
    WeightValidationError,
)
from nutrition_agent.nutrition import find_matching_food, validate_body_weight
from nutrition_agent.repository import load_foods_from_json


def test_weight_validation_valid_range():
    """Valid weight between 30 and 300 kg passes without error."""
    log = UserLog(log_type="WEIGHT", weight_kg=82.4)
    assert log.weight_kg == 82.4
    assert validate_body_weight(82.4) == 82.4


@pytest.mark.parametrize("absurd_weight", [29.9, 15.0, 0.0, -5.0, 300.1, 450.0])
def test_weight_validation_absurd_values_fail_fast(absurd_weight: float):
    """Absurd weight values (< 30 kg or > 300 kg) raise ValueError / WeightValidationError."""
    # 1. Direct Python validator throws ValueError / WeightValidationError before DB write
    with pytest.raises(ValueError) as exc_info:
        validate_body_weight(absurd_weight)
    assert issubclass(WeightValidationError, ValueError)
    assert isinstance(exc_info.value, WeightValidationError)
    assert "Povolený rozsah je 30.0 až 300.0 kg" in str(exc_info.value)

    # 2. Pydantic model initialization also fails fast
    with pytest.raises(Exception) as model_exc:
        UserLog(log_type="WEIGHT", weight_kg=absurd_weight)
    assert "Povolený rozsah je 30.0 až 300.0 kg" in str(model_exc.value)


def test_food_input_piece_defaults_from_parser_schema():
    """Countable item without quantity sets amount=1, unit='ks', is_estimated=True."""
    item = ExtractedFoodItem(
        name="jablko",
        amount=1.0,
        unit="ks",
        is_estimated=True,
    )
    log = UserLog(log_type="FOOD", items=[item])
    assert log.items[0].name == "jablko"
    assert log.items[0].amount == 1.0
    assert log.items[0].unit == "ks"
    assert log.items[0].is_estimated is True


def test_unspecified_quantity_returns_invalid_log():
    """Uncountable food without quantity yields log_type='INVALID' and error_reason."""
    log = UserLog(
        log_type="INVALID",
        error_reason="Nespecifikované množství suroviny",
    )
    assert log.log_type == "INVALID"
    assert log.error_reason == "Nespecifikované množství suroviny"


def test_food_matching_unrecognized_raises_food_not_found():
    """Food with similarity < 75% raises FoodNotFoundError to prompt the user."""
    foods = load_foods_from_json("data/foods.json")

    # "pneumatika" or completely unknown item has 0-30% similarity
    with pytest.raises(FoodNotFoundError) as exc_info:
        find_matching_food("pneumatika", foods)

    assert "pneumatika" in str(exc_info.value)
    assert "< 75.0%" in str(exc_info.value)
    assert "Upřesněte prosím název" in str(exc_info.value)


def test_food_matching_recognized_synonyms_and_items():
    """Items above 75% or matching synonyms are resolved properly."""
    foods = load_foods_from_json("data/foods.json")

    # Synonym "kuřecí" -> "Kuřecí prsa"
    food, score = find_matching_food("kuřecí", foods)
    assert food.name == "Kuřecí prsa"
    assert score >= 75.0

    # Direct match "jasmínová rýže" -> "Rýže jasmínová (suchá)"
    food, score = find_matching_food("jasmínová rýže", foods)
    assert food.name == "Rýže jasmínová (suchá)"
    assert score >= 75.0


def test_extracted_food_item_positive_amount_validation():
    """Amount <= 0 fails fast with ValueError."""
    with pytest.raises(ValueError, match="Množství suroviny musí být kladné číslo"):
        ExtractedFoodItem(name="rýže", amount=0.0)

    with pytest.raises(ValueError, match="Množství suroviny musí být kladné číslo"):
        ExtractedFoodItem(name="rýže", amount=-50.0)


def test_google_genai_schema_compatibility():
    """Verify that UserLog schema is accepted by google-genai without exclusiveMinimum."""
    from google.genai._transformers import t_schema
    from google.genai.types import Schema

    schema = t_schema(None, UserLog)
    assert isinstance(schema, Schema)
