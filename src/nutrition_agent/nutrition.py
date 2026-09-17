"""Core nutrition calculation, DIAAS analysis, and fuzzy matching logic."""

import logging
from enum import StrEnum
from typing import Literal

from rapidfuzz import fuzz

from nutrition_agent.models import (
    MAX_WEIGHT_KG,
    MIN_FOOD_MATCH_SCORE,
    MIN_WEIGHT_KG,
    CalculatedFoodLog,
    ExtractedFoodItem,
    FoodDefinition,
    FoodNotFoundError,
    WeightValidationError,
)

logger = logging.getLogger(__name__)


class ProteinCategory(StrEnum):
    """Categories of protein sources with typical DIAAS score profiles."""

    ANIMAL = "animal"
    PLANT_ISOLATE = "plant_isolate"
    LEGUMES = "legumes"
    GRAINS_SEEDS = "grains_seeds"
    NUTS_PEANUTS = "nuts_peanuts"


# Typical DIAAS coefficients according to spec
DIAAS_COEFFICIENTS: dict[ProteinCategory, float] = {
    ProteinCategory.ANIMAL: 1.00,
    ProteinCategory.PLANT_ISOLATE: 0.85,
    ProteinCategory.LEGUMES: 0.75,
    ProteinCategory.GRAINS_SEEDS: 0.70,
    ProteinCategory.NUTS_PEANUTS: 0.65,
}

# Conservative default coefficient if not explicitly provided
DEFAULT_DIAAS_COEFFICIENT: float = 0.70

# Hard synonyms mapping before metric execution (vault.md section 3)
FOOD_SYNONYMS: dict[str, str] = {
    "kuřecí": "kuřecí prsa",
    "hovězí": "hovězí zadní",
    "vločky": "ovesné vločky",
    "protein": "syrovátkový protein (80%)",
    "tvaroh": "tvaroh odtučněný",
}

# Standard piece and measure weights in grams (vault.md section 2)
STANDARD_UNIT_WEIGHTS_GRAMS: dict[str, float] = {
    "vejce": 55.0,
    "vejce (celé)": 55.0,
    "banán": 120.0,
    "jablko": 150.0,
    "lžíce oleje": 13.0,
    "lžička oleje": 5.0,
    "odměrka proteinu": 30.0,
}

# Cooking conversion ratios: multiplier from cooked weight to raw equivalent weight.
# Meat/fish lose ~20-25% water (yield ~0.75-0.80), so 100g cooked = ~1.25g raw.
# Grains/legumes absorb water (yield ~2.3-2.5x), so 100g cooked = ~0.40g dry.
COOKED_TO_RAW_RATIOS: dict[str, float] = {
    "kuřecí prsa": 1.25,
    "hovězí zadní": 1.30,
    "losos": 1.20,
    "rýže jasmínová (suchá)": 0.40,
    "těstoviny semolinové": 0.43,
    "ovesné vločky": 0.35,
    "čočka hnědá (suchá)": 0.43,
    "cizrna (suchá)": 0.43,
    "brambory": 1.00,
    "tofu bílé": 1.00,
    "vejce (celé)": 1.00,
    "jablko": 1.00,
    "banán": 1.00,
}


def get_cooked_to_raw_ratio(food_name: str) -> float:
    """Return conversion ratio from cooked weight to raw equivalent weight."""
    normalized = food_name.strip().lower()
    for key, ratio in COOKED_TO_RAW_RATIOS.items():
        if key in normalized or normalized in key:
            return ratio
    return 1.0


def get_coefficient_for_category(category: ProteinCategory | str) -> float:
    """Return DIAAS coefficient for a given protein category.

    Defaults to DEFAULT_DIAAS_COEFFICIENT (0.70) if category is unknown.
    """
    if isinstance(category, str):
        try:
            category = ProteinCategory(category.lower())
        except ValueError:
            return DEFAULT_DIAAS_COEFFICIENT
    return DIAAS_COEFFICIENTS.get(category, DEFAULT_DIAAS_COEFFICIENT)


def calculate_usable_protein(
    protein_raw: float,
    coefficient: float | None = None,
) -> float:
    """Calculate usable protein amount based on raw protein and DIAAS coefficient.

    Formula:
        Protein_usable = Protein_raw * Koeficient

    Args:
        protein_raw: Raw protein quantity in grams.
        coefficient: DIAAS coefficient (0.0 to 1.0+). Defaults to 0.70 if None.

    Returns:
        Usable protein quantity in grams, rounded to 2 decimal places.
    """
    if protein_raw < 0:
        raise ValueError("Raw protein amount cannot be negative.")

    coeff = DEFAULT_DIAAS_COEFFICIENT if coefficient is None else coefficient
    if coeff < 0:
        raise ValueError("DIAAS coefficient cannot be negative.")

    return round(protein_raw * coeff, 2)


def validate_body_weight(weight_kg: float) -> float:
    """Validate that logged weight falls into realistic interval [30.0, 300.0] kg (Fail Fast)."""
    if weight_kg < MIN_WEIGHT_KG or weight_kg > MAX_WEIGHT_KG:
        raise WeightValidationError(weight_kg)
    return weight_kg


def find_matching_food(
    query: str,
    foods: list[FoodDefinition],
) -> tuple[FoodDefinition, float]:
    """Find the best matching food in database using rapidfuzz token_sort_ratio.

    Rules:
        - Score >= 85: Immediate automatic match.
        - Score 75 - 84: Acceptable match with log warning.
        - Score < 75: Match rejected, raises FoodNotFoundError (Fail Fast).

    Args:
        query: Raw or extracted food name.
        foods: List of food definitions from local foods.json database.

    Returns:
        Tuple of (Matched FoodDefinition, score).

    Raises:
        FoodNotFoundError: If best matching score is below 75%.
    """
    normalized_query = query.strip().lower()

    # Check hard synonyms dictionary first
    matched_query = FOOD_SYNONYMS.get(normalized_query, normalized_query)

    best_food: FoodDefinition | None = None
    best_score: float = -1.0

    for food in foods:
        candidate_name = food.name.strip().lower()
        score = float(fuzz.token_sort_ratio(matched_query, candidate_name))
        if score > best_score:
            best_score = score
            best_food = food

    if best_food is None or best_score < MIN_FOOD_MATCH_SCORE:
        best_name = best_food.name if best_food else None
        raise FoodNotFoundError(
            food_name=query,
            best_match=best_name,
            score=max(0.0, best_score),
        )

    if best_score < 85.0:
        logger.warning(
            "Nejisté mapování potraviny: '%s' -> '%s' (skóre %.1f%%)",
            query,
            best_food.name,
            best_score,
        )

    return best_food, best_score


def convert_to_grams(
    food_name: str,
    amount: float,
    unit: Literal["g", "ml", "ks", "porce"] | str,
) -> float:
    """Convert given unit and amount to weight in grams.

    Uses standard piece/measure conversions from vault.md.
    """
    if amount <= 0:
        raise ValueError("Množství musí být kladné číslo.")

    unit_norm = unit.strip().lower()
    food_norm = food_name.strip().lower()

    if unit_norm == "g":
        return amount
    if unit_norm == "ml":
        # Liquids: default to 1 ml ~= 1 g unless specific density needed
        return amount

    if unit_norm == "ks":
        # Check piece table for matching item
        for key, weight in STANDARD_UNIT_WEIGHTS_GRAMS.items():
            if key in food_norm or food_norm in key:
                return amount * weight
        # Default fallback for unlisted 1 piece is 100g
        return amount * 100.0

    if unit_norm == "porce":
        # Default single portion estimation
        return amount * 150.0

    return amount


def calculate_food_log(
    item: ExtractedFoodItem,
    food_def: FoodDefinition,
    raw_input: str,
) -> CalculatedFoodLog:
    """Calculate scaled macronutrients, calories, and DIAAS protein for an item.

    Converts cooked foods to their raw-equivalent weight when computing nutrition
    against the raw food database.
    """
    amount_g = convert_to_grams(food_def.name, item.amount, item.unit)

    if item.state == "cooked":
        ratio = get_cooked_to_raw_ratio(food_def.name)
        raw_equivalent_g = amount_g * ratio
    else:
        raw_equivalent_g = amount_g

    multiplier = raw_equivalent_g / 100.0

    calories = round(food_def.calories * multiplier, 1)
    protein_raw = round(food_def.protein * multiplier, 2)
    protein_usable = calculate_usable_protein(protein_raw, food_def.diaas_coefficient)
    carbs = round(food_def.carbs * multiplier, 2)
    fats = round(food_def.fats * multiplier, 2)

    return CalculatedFoodLog(
        raw_input=raw_input,
        food_name_matched=food_def.name,
        amount_g=round(amount_g, 1),
        state=item.state,
        is_estimated=item.is_estimated,
        calories=calories,
        protein_raw=protein_raw,
        protein_usable=protein_usable,
        carbs=carbs,
        fats=fats,
    )
