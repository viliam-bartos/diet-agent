"""Tests for DIAAS usable protein calculations."""

import pytest

from nutrition_agent.nutrition import (
    DEFAULT_DIAAS_COEFFICIENT,
    DIAAS_COEFFICIENTS,
    ProteinCategory,
    calculate_usable_protein,
    get_coefficient_for_category,
)


def test_diaas_coefficients_mapping():
    """Verify DIAAS coefficients match the project specification."""
    assert DIAAS_COEFFICIENTS[ProteinCategory.ANIMAL] == 1.00
    assert DIAAS_COEFFICIENTS[ProteinCategory.PLANT_ISOLATE] == 0.85
    assert DIAAS_COEFFICIENTS[ProteinCategory.LEGUMES] == 0.75
    assert DIAAS_COEFFICIENTS[ProteinCategory.GRAINS_SEEDS] == 0.70
    assert DIAAS_COEFFICIENTS[ProteinCategory.NUTS_PEANUTS] == 0.65
    assert DEFAULT_DIAAS_COEFFICIENT == 0.70


def test_get_coefficient_for_category():
    """Verify coefficient resolution by category and fallback."""
    assert get_coefficient_for_category(ProteinCategory.ANIMAL) == 1.00
    assert get_coefficient_for_category("animal") == 1.00
    assert get_coefficient_for_category("legumes") == 0.75
    assert get_coefficient_for_category("unknown_category") == 0.70


@pytest.mark.parametrize(
    ("protein_raw", "coeff", "expected_usable"),
    [
        (23.0, 1.00, 23.0),  # Chicken breast
        (12.0, 0.85, 10.2),  # Tofu
        (24.0, 0.75, 18.0),  # Lentils
        (13.5, 0.70, 9.45),  # Rolled oats
        (25.0, 0.65, 16.25),  # Peanut butter
        (0.0, 1.00, 0.0),  # Zero protein
    ],
)
def test_calculate_usable_protein_with_explicit_coefficient(
    protein_raw: float, coeff: float, expected_usable: float
):
    """Test calculation with explicit coefficient."""
    assert calculate_usable_protein(protein_raw, coeff) == expected_usable


def test_calculate_usable_protein_default_fallback():
    """If no coefficient is provided, fallback to 0.70 must be used."""
    raw = 20.0
    expected = round(20.0 * 0.70, 2)  # 14.0
    assert calculate_usable_protein(raw, None) == expected


def test_calculate_usable_protein_negative_inputs():
    """Verify error handling for negative values."""
    with pytest.raises(ValueError, match="Raw protein amount cannot be negative"):
        calculate_usable_protein(-10.0, 1.0)

    with pytest.raises(ValueError, match="DIAAS coefficient cannot be negative"):
        calculate_usable_protein(20.0, -0.5)


def test_cooked_to_raw_ratio_lookup():
    """Verify cooking yield ratios for meat (water loss) and grains (water absorption)."""
    from nutrition_agent.nutrition import get_cooked_to_raw_ratio

    # Chicken loses water, raw equivalent is higher
    assert get_cooked_to_raw_ratio("Kuřecí prsa") == 1.25
    # Rice absorbs water, raw equivalent is lower
    assert get_cooked_to_raw_ratio("Rýže jasmínová (suchá)") == 0.40
    # Unknown item defaults to 1.0
    assert get_cooked_to_raw_ratio("Neznámá surovina") == 1.00


def test_calculate_food_log_cooked_state_adjustment():
    """Verify cooked foods are scaled through raw equivalent weight."""
    from nutrition_agent.models import ExtractedFoodItem, FoodDefinition
    from nutrition_agent.nutrition import calculate_food_log

    rice_def = FoodDefinition(
        name="Rýže jasmínová (suchá)",
        calories=355.0,
        protein=7.0,
        diaas_coefficient=0.70,
        carbs=79.0,
        fats=0.7,
    )

    # 150g raw rice: multiplier = 1.5 -> 355 * 1.5 = 532.5 kcal
    raw_item = ExtractedFoodItem(name="rýže", amount=150.0, unit="g", state="raw")
    raw_log = calculate_food_log(raw_item, rice_def, "150g suché rýže", amount_g=150.0)
    assert raw_log.calories == 532.5
    assert raw_log.protein_raw == 10.5

    # 150g cooked rice: 150 * 0.40 = 60g raw equivalent -> 355 * 0.6 = 213.0 kcal
    cooked_item = ExtractedFoodItem(name="rýže", amount=150.0, unit="g", state="cooked")
    cooked_log = calculate_food_log(cooked_item, rice_def, "150g vařené rýže", amount_g=150.0)
    assert cooked_log.calories == 213.0
    assert cooked_log.protein_raw == 4.2
    assert cooked_log.state == "cooked"
    assert cooked_log.amount_g == 150.0
    assert cooked_log.conversion_estimated is False


def test_calculate_food_log_marks_estimated_conversion():
    """The conversion flag survives into the calculated log."""
    from nutrition_agent.models import ExtractedFoodItem, FoodDefinition
    from nutrition_agent.nutrition import calculate_food_log

    food = FoodDefinition(name="Test", calories=100.0, protein=1.0, carbs=1.0, fats=1.0)
    item = ExtractedFoodItem(name="test", amount=2.0, unit="porce", state="cooked")

    log = calculate_food_log(item, food, "2 porce", amount_g=600.0, conversion_estimated=True)
    assert log.conversion_estimated is True
    assert log.amount_g == 600.0


def test_calculate_food_log_rejects_non_positive_grams():
    """Zero grams would silently produce an empty log, so it fails fast."""
    from nutrition_agent.models import ExtractedFoodItem, FoodDefinition
    from nutrition_agent.nutrition import calculate_food_log

    food = FoodDefinition(name="Test", calories=100.0, protein=1.0, carbs=1.0, fats=1.0)
    item = ExtractedFoodItem(name="test", amount=1.0, unit="ks", state="raw")

    with pytest.raises(ValueError, match="musí být kladné číslo"):
        calculate_food_log(item, food, "1 ks", amount_g=0.0)


def _stored_log():
    from nutrition_agent.models import CalculatedFoodLog

    return CalculatedFoodLog(
        id=1,
        raw_input="300g vařené rýže",
        food_name_matched="Rýže jasmínová (suchá)",
        amount_g=300.0,
        state="cooked",
        is_estimated=False,
        calories=426.0,
        protein_raw=8.4,
        protein_usable=5.88,
        carbs=94.8,
        fats=0.84,
        alcohol=0.0,
        nutrition_source="curated",
    )


def test_apply_manual_macros_derives_raw_protein_back():
    """The dialog shows usable protein, so the raw one is derived through DIAAS."""
    from nutrition_agent.models import FoodDefinition
    from nutrition_agent.nutrition import apply_manual_macros

    rice = FoodDefinition(
        name="Rýže jasmínová (suchá)",
        calories=355.0,
        protein=7.0,
        diaas_coefficient=0.70,
        carbs=79.0,
        fats=0.7,
    )

    updated = apply_manual_macros(
        _stored_log(),
        rice,
        300.0,
        "cooked",
        calories=500.0,
        protein_usable=14.0,
        carbs=100.0,
        fats=2.0,
    )

    assert updated.calories == 500.0
    assert updated.protein_usable == 14.0
    assert updated.protein_raw == 20.0  # 14 / 0.70
    assert updated.carbs == 100.0
    assert updated.nutrition_source == "manual"
    assert updated.id == 1
    assert updated.raw_input == "300g vařené rýže"


def test_apply_manual_macros_keeps_stored_alcohol():
    """Alcohol is not editable in the dialog, so it must survive a macro fix."""
    from nutrition_agent.models import FoodDefinition
    from nutrition_agent.nutrition import apply_manual_macros

    beer = FoodDefinition(name="Pivo", calories=43.0, protein=0.5, carbs=3.5, fats=0.0, alcohol=4.0)
    stored = _stored_log().model_copy(update={"alcohol": 12.0})

    updated = apply_manual_macros(
        stored, beer, 300.0, "raw", calories=129.0, protein_usable=1.5, carbs=10.5, fats=0.0
    )
    assert updated.alcohol == 12.0

    overridden = apply_manual_macros(
        stored,
        beer,
        300.0,
        "raw",
        calories=129.0,
        protein_usable=1.5,
        carbs=10.5,
        fats=0.0,
        alcohol=13.0,
    )
    assert overridden.alcohol == 13.0


def test_apply_manual_macros_rejects_non_positive_amount():
    """A manual correction still needs a usable weight."""
    from nutrition_agent.models import FoodDefinition
    from nutrition_agent.nutrition import apply_manual_macros

    food = FoodDefinition(name="Test", calories=100.0, protein=1.0, carbs=1.0, fats=1.0)

    with pytest.raises(ValueError, match="musí být kladné číslo"):
        apply_manual_macros(
            _stored_log(), food, 0.0, "raw", calories=1.0, protein_usable=1.0, carbs=1.0, fats=1.0
        )


def test_per_100g_from_portion_converts_cooked_grams_to_raw_basis():
    """A correction typed for a cooked portion is scaled back through the yield ratio."""
    from nutrition_agent.nutrition import per_100g_from_portion

    # Raw: 200 g portion -> half of the per-100 g value.
    assert per_100g_from_portion(300.0, 200.0, "raw", "Kuřecí prsa") == 150.0
    # Cooked chicken: 200 g cooked is 250 g raw equivalent -> 40% of the value.
    assert per_100g_from_portion(500.0, 200.0, "cooked", "Kuřecí prsa") == 200.0
    # Cooked rice: 300 g cooked is 120 g dry -> 250% of the value.
    assert per_100g_from_portion(142.0, 300.0, "cooked", "Rýže jasmínová (suchá)") == 118.33

    with pytest.raises(ValueError, match="musí být kladné číslo"):
        per_100g_from_portion(100.0, 0.0, "raw", "Test")
