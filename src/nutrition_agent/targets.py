"""Nutritional goal states, day types, and dynamic target calculations."""

from dataclasses import dataclass
from enum import StrEnum


class DietGoal(StrEnum):
    """Dietary goal states."""

    MAINTENANCE = "maintenance"
    LEAN_BULK = "lean_bulk"
    CUT = "cut"


class DayType(StrEnum):
    """Daily activity level / training status."""

    TRAINING = "training"
    REST = "rest"


@dataclass(frozen=True)
class TargetRatios:
    """Macronutrient and energy ratios per kilogram of body weight."""

    calories_per_kg: float
    protein_per_kg: float
    fats_per_kg: float
    carbs_per_kg: float


# Base ratios calibrated against the 83.0 kg specification
GOAL_RATIOS: dict[tuple[DietGoal, DayType], TargetRatios] = {
    # 1. Maintenance
    (DietGoal.MAINTENANCE, DayType.TRAINING): TargetRatios(
        calories_per_kg=2750.0 / 83.0,
        protein_per_kg=150.0 / 83.0,
        fats_per_kg=82.5 / 83.0,
        carbs_per_kg=320.0 / 83.0,
    ),
    (DietGoal.MAINTENANCE, DayType.REST): TargetRatios(
        calories_per_kg=2475.0 / 83.0,
        protein_per_kg=145.0 / 83.0,
        fats_per_kg=80.0 / 83.0,
        carbs_per_kg=310.0 / 83.0,
    ),
    # 2. Lean Bulk (default active mode)
    (DietGoal.LEAN_BULK, DayType.TRAINING): TargetRatios(
        calories_per_kg=3150.0 / 83.0,
        protein_per_kg=155.0 / 83.0,
        fats_per_kg=82.5 / 83.0,
        carbs_per_kg=430.0 / 83.0,
    ),
    (DietGoal.LEAN_BULK, DayType.REST): TargetRatios(
        calories_per_kg=2650.0 / 83.0,
        protein_per_kg=145.0 / 83.0,
        fats_per_kg=77.5 / 83.0,
        carbs_per_kg=340.0 / 83.0,
    ),
    # 3. Cut
    (DietGoal.CUT, DayType.TRAINING): TargetRatios(
        calories_per_kg=2200.0 / 83.0,
        protein_per_kg=175.0 / 83.0,
        fats_per_kg=68.0 / 83.0,
        carbs_per_kg=215.0 / 83.0,
    ),
    (DietGoal.CUT, DayType.REST): TargetRatios(
        calories_per_kg=2150.0 / 83.0,
        protein_per_kg=175.0 / 83.0,
        fats_per_kg=65.0 / 83.0,
        carbs_per_kg=205.0 / 83.0,
    ),
}


@dataclass(frozen=True)
class DailyTargets:
    """Calculated daily macronutrient and energy targets."""

    weight_kg: float
    goal: DietGoal
    day_type: DayType
    calories: float
    protein_usable: float
    protein_total: float
    fats: float
    carbs: float


def parse_goal(value: str) -> DietGoal:
    """Parse user text input into DietGoal enum."""
    val = value.strip().lower()
    if val in ("bulk", "lean_bulk", "lean bulk", "nabirani", "objem"):
        return DietGoal.LEAN_BULK
    if val in ("maintenance", "udrzba", "udrzovani"):
        return DietGoal.MAINTENANCE
    if val in ("cut", "rysovani", "redukce", "hubnuti", "dieta"):
        return DietGoal.CUT
    raise ValueError(f"Neznámý cíl: '{value}'. Možnosti: lean_bulk, maintenance, cut.")


def parse_day_type(value: str) -> DayType:
    """Parse user text input into DayType enum."""
    val = value.strip().lower()
    if val in ("trenink", "training", "workout", "trénink"):
        return DayType.TRAINING
    if val in ("odpocinek", "odpočinek", "rest", "volno"):
        return DayType.REST
    raise ValueError(f"Neznámý typ dne: '{value}'. Možnosti: trenink, odpocinek.")


def calculate_daily_targets(
    weight_kg: float,
    goal: DietGoal = DietGoal.LEAN_BULK,
    day_type: DayType = DayType.TRAINING,
) -> DailyTargets:
    """Calculate target calories and macros for given body weight, goal, and day type."""
    if weight_kg <= 0:
        raise ValueError("Hmotnost musí být kladné číslo.")

    ratios = GOAL_RATIOS.get(
        (goal, day_type),
        GOAL_RATIOS[(DietGoal.LEAN_BULK, DayType.TRAINING)],
    )

    calories = round(weight_kg * ratios.calories_per_kg, 1)
    protein_usable = round(weight_kg * ratios.protein_per_kg, 1)
    protein_total = round(protein_usable * 1.1, 1)
    fats = round(weight_kg * ratios.fats_per_kg, 1)
    carbs = round(weight_kg * ratios.carbs_per_kg, 1)

    return DailyTargets(
        weight_kg=round(weight_kg, 1),
        goal=goal,
        day_type=day_type,
        calories=calories,
        protein_usable=protein_usable,
        protein_total=protein_total,
        fats=fats,
        carbs=carbs,
    )
