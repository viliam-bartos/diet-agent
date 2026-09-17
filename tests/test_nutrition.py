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
