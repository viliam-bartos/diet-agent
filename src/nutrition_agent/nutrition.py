"""Core nutrition calculation, DIAAS analysis, and fuzzy matching logic."""

import logging
import re
import unicodedata
from enum import StrEnum

from rapidfuzz import fuzz

from nutrition_agent.models import (
    MAX_WEIGHT_KG,
    MIN_FOOD_MATCH_SCORE,
    MIN_WEIGHT_KG,
    CalculatedFoodLog,
    EstimatedFoodNutrition,
    ExtractedFoodItem,
    FoodDefinition,
    FoodNotFoundError,
    WeightValidationError,
)

logger = logging.getLogger(__name__)


def strip_diacritics(text: str) -> str:
    """Remove diacritics so 'maslo' and 'máslo' compare as the same word."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


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
    "rýže": "rýže jasmínová (suchá)",
    "čočka": "čočka hnědá (suchá)",
    "cizrna": "cizrna (suchá)",
    "těstoviny": "těstoviny semolinové",
    "vejce": "vejce (celé)",
    "olej": "olivový olej",
    "tofu": "tofu bílé",
}

# Same synonyms keyed without diacritics, so 'ryze' resolves like 'rýže'.
FOLDED_SYNONYMS: dict[str, str] = {
    strip_diacritics(key): value for key, value in FOOD_SYNONYMS.items()
}

# Weights that depend on the specific food, per countable piece.
PIECE_WEIGHTS_GRAMS: dict[str, float] = {
    "vejce": 55.0,
    "vejce (celé)": 55.0,
    "banán": 120.0,
    "jablko": 150.0,
}

# Household measures with a weight that does not depend on the food.
# Anything missing here has to be estimated or filled in by the user, never guessed.
GENERIC_MEASURE_WEIGHTS_GRAMS: dict[str, float] = {
    "lžíce": 13.0,
    "lžíce oleje": 13.0,
    "lžička": 5.0,
    "lžička oleje": 5.0,
    "hrnek": 240.0,
    "odměrka": 30.0,
    "odměrka proteinu": 30.0,
    "plátek": 20.0,
    "krajíc": 40.0,
}

# Units that describe an absolute amount and need no conversion table.
ABSOLUTE_UNITS: frozenset[str] = frozenset({"g", "ml"})

# Units whose grams depend on the food, not on a fixed household measure.
FOOD_DEPENDENT_UNITS: frozenset[str] = frozenset({"ks", "porce"})

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


def food_tokens(name: str) -> set[str]:
    """Split a food name into comparable lowercase tokens, ignoring punctuation.

    Tokens are diacritic-folded because people often type Czech names without
    diacritics, especially on mobile.
    """
    folded = strip_diacritics(name.lower())
    return {token for token in re.split(r"[^\w]+", folded) if token}


def is_token_subset(query: str, candidate: str) -> bool:
    """Report whether every word of the query appears in the candidate name.

    'máslo' is a subset of 'Máslo 82%' and 'cfm syrovátkový protein' is a subset of
    'Prom-IN CFM Pure Performance (syrovátkový protein)'. Those pairs score below
    the similarity threshold even though they clearly describe the same food.
    """
    query_tokens = food_tokens(query)
    return bool(query_tokens) and query_tokens <= food_tokens(candidate)


def find_matching_food(
    query: str,
    foods: list[FoodDefinition],
) -> tuple[FoodDefinition, float]:
    """Find the best matching food in the catalog.

    Rules:
        - Score >= 85: Immediate automatic match.
        - Score 75 - 84: Acceptable match with log warning.
        - All query words contained in the candidate name: accepted, still scored
          by token_sort_ratio so an uncertain mapping is logged as such.
        - Otherwise: rejected with FoodNotFoundError (Fail Fast).

    A curated catalog entry always wins over an AI-estimated one whenever both are
    acceptable, so an estimate can never shadow a verified food.

    Returns:
        Tuple of (Matched FoodDefinition, score).

    Raises:
        FoodNotFoundError: If no candidate is acceptable.
    """
    normalized_query = " ".join(query.strip().lower().split())

    # Hard synonyms first, matched with and without diacritics.
    matched_query = (
        FOOD_SYNONYMS.get(normalized_query)
        or FOLDED_SYNONYMS.get(strip_diacritics(normalized_query))
        or normalized_query
    )

    acceptable: list[tuple[FoodDefinition, float, bool]] = []
    best_food: FoodDefinition | None = None
    best_score: float = -1.0

    for food in foods:
        candidate_name = food.name.strip().lower()
        score = float(fuzz.token_sort_ratio(matched_query, candidate_name))

        if score > best_score:
            best_score = score
            best_food = food

        contained = is_token_subset(matched_query, candidate_name)
        if score >= MIN_FOOD_MATCH_SCORE or contained:
            acceptable.append((food, score, contained))

    if not acceptable:
        raise FoodNotFoundError(
            food_name=query,
            best_match=best_food.name if best_food else None,
            score=max(0.0, best_score),
        )

    # A curated entry always beats an estimate, a full word match beats a vague one,
    # and only then does the similarity score decide.
    chosen_food, chosen_score, _ = max(
        acceptable,
        key=lambda candidate: (
            candidate[0].source == "curated",
            candidate[2],
            candidate[1],
        ),
    )

    if chosen_score < 85.0:
        logger.warning(
            "Nejisté mapování potraviny: '%s' -> '%s' (skóre %.1f%%)",
            query,
            chosen_food.name,
            chosen_score,
        )

    return chosen_food, chosen_score


def lookup_unit_weight(food_name: str, unit: str) -> float | None:
    """Return grams for one unit of a food, or None when nothing is known.

    Piece weights tied to a specific food win over the generic measure table, so
    '1 ks vejce' uses 55 g while '1 plátek šunky' uses the generic measure.
    """
    food_norm = food_name.strip().lower()
    unit_norm = unit.strip().lower()

    if unit_norm == "ks":
        for key, weight in PIECE_WEIGHTS_GRAMS.items():
            if key in food_norm or food_norm in key:
                return weight

    return GENERIC_MEASURE_WEIGHTS_GRAMS.get(unit_norm)


def convert_to_grams(
    food_name: str,
    amount: float,
    unit: str,
) -> float | None:
    """Convert an amount and unit into grams, or None when the conversion is unknown.

    Returning None instead of a made-up default keeps invented weights out of the
    database; the caller decides whether to estimate or ask the user.
    """
    if amount <= 0:
        raise ValueError("Množství musí být kladné číslo.")

    unit_norm = unit.strip().lower()

    # 1 ml of a water-based liquid is treated as 1 g.
    if unit_norm in ABSOLUTE_UNITS:
        return amount

    unit_weight = lookup_unit_weight(food_name, unit_norm)
    if unit_weight is None:
        return None

    return amount * unit_weight


def calculate_food_log(
    item: ExtractedFoodItem,
    food_def: FoodDefinition,
    raw_input: str,
    amount_g: float,
    conversion_estimated: bool = False,
) -> CalculatedFoodLog:
    """Calculate scaled macronutrients, calories, and DIAAS protein for an item.

    Converts cooked foods to their raw-equivalent weight when computing nutrition
    against the raw food database. Grams must already be resolved by the caller,
    which keeps unit conversion and estimation out of the arithmetic.
    """
    if amount_g <= 0:
        raise ValueError("Hmotnost potraviny musí být kladné číslo.")

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
    alcohol = round(food_def.alcohol * multiplier, 2)

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
        alcohol=alcohol,
        nutrition_source=food_def.source,
        conversion_estimated=conversion_estimated,
    )


def recalculate_food_log(
    existing: CalculatedFoodLog,
    food_def: FoodDefinition,
    amount_g: float,
    state: str,
) -> CalculatedFoodLog:
    """Rebuild a stored food log from a new amount, keeping macros consistent.

    Editing only the amount would leave calories and protein stale, so the whole
    log is recomputed from the food definition instead.
    """
    if amount_g <= 0:
        raise ValueError("Hmotnost potraviny musí být kladné číslo.")

    if state == "cooked":
        raw_equivalent_g = amount_g * get_cooked_to_raw_ratio(food_def.name)
    else:
        raw_equivalent_g = amount_g

    multiplier = raw_equivalent_g / 100.0
    protein_raw = round(food_def.protein * multiplier, 2)

    return existing.model_copy(
        update={
            "food_name_matched": food_def.name,
            "amount_g": round(amount_g, 1),
            "state": state,
            "calories": round(food_def.calories * multiplier, 1),
            "protein_raw": protein_raw,
            "protein_usable": calculate_usable_protein(protein_raw, food_def.diaas_coefficient),
            "carbs": round(food_def.carbs * multiplier, 2),
            "fats": round(food_def.fats * multiplier, 2),
            "alcohol": round(food_def.alcohol * multiplier, 2),
            "nutrition_source": food_def.source,
        }
    )


def build_food_from_estimate(estimate: EstimatedFoodNutrition) -> FoodDefinition:
    """Turn an LLM nutrition estimate into a food definition flagged as AI-sourced."""
    if estimate.calories <= 0 and estimate.protein <= 0 and estimate.carbs <= 0:
        raise ValueError(f"Model nezná potravinu '{estimate.name}' a nevrátil použitelné hodnoty.")

    return FoodDefinition(
        name=estimate.name.strip().lower(),
        calories=round(estimate.calories, 1),
        protein=round(estimate.protein, 2),
        carbs=round(estimate.carbs, 2),
        fats=round(estimate.fats, 2),
        alcohol=round(estimate.alcohol, 2),
        diaas_coefficient=get_coefficient_for_category(estimate.protein_category),
        source="ai",
    )
