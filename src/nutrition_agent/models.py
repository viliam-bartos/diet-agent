"""Data models, schemas, and custom exceptions for Nutrition Agent."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Domain constants
MIN_WEIGHT_KG: float = 30.0
MAX_WEIGHT_KG: float = 300.0
MIN_FOOD_MATCH_SCORE: float = 75.0


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


class FoodNotFoundError(Exception):
    """Raised when food matching score in foods database is below threshold (< 75%)."""

    def __init__(self, food_name: str, best_match: str | None = None, score: float = 0.0) -> None:
        self.food_name = food_name
        self.best_match = best_match
        self.score = score
        message = (
            f"Surovina '{food_name}' nebyla nalezena v databázi (nejlepší shoda: "
            f"'{best_match}' se skóre {score:.1f}% < {MIN_FOOD_MATCH_SCORE}%). "
            f"Upřesněte prosím název nebo zadejte nutriční hodnoty ručně."
        )
        super().__init__(message)


class WeightValidationError(ValueError):
    """Raised when logged body weight is outside realistic physiological bounds [30.0, 300.0] kg."""

    def __init__(self, weight_kg: float) -> None:
        self.weight_kg = weight_kg
        super().__init__(
            f"Neplatná hodnota tělesné hmotnosti {weight_kg} kg. "
            f"Povolený rozsah je {MIN_WEIGHT_KG} až {MAX_WEIGHT_KG} kg."
        )


class LogType(StrEnum):
    """Types of logged input."""

    FOOD = "FOOD"
    WEIGHT = "WEIGHT"
    INVALID = "INVALID"


class FoodState(StrEnum):
    """Cooking state of food item."""

    RAW = "raw"
    COOKED = "cooked"


class ExtractedFoodItem(BaseModel):
    """Food item extracted by LLM parser."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(
        ...,
        description="Standardizovaný název potraviny v češtině, např. 'kuřecí prsa', 'rýže'",
    )
    amount: float = Field(
        ...,
        gt=0,
        description="Numerické množství suroviny v gramech, ml nebo kusech",
    )
    unit: Literal["g", "ml", "ks", "porce"] = Field(
        default="g",
        description="Jednotka množství; pokud chybí, použij 'g'",
    )
    state: Literal["raw", "cooked"] = Field(
        default="raw",
        description="Stav potraviny: 'raw' (syrový stav) nebo 'cooked' (tepelně upravené jídlo)",
    )
    is_estimated: bool = Field(
        default=False,
        description="True, pokud gramáž nebyla v textu explicitně uvedena číslem",
    )


class UserLog(BaseModel):
    """Strict structured JSON response from LLM parser."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    log_type: Literal["FOOD", "WEIGHT", "INVALID"] = Field(
        ...,
        description="Typ záznamu: FOOD pro jídlo, WEIGHT pro hmotnost, INVALID při chybě",
    )
    items: list[ExtractedFoodItem] = Field(
        default_factory=list,
        description="Seznam extrahovaných položek jídla (pouze pro log_type='FOOD')",
    )
    weight_kg: float | None = Field(
        default=None,
        description="Zaznamenaná tělesná váha v kg (pouze pro log_type='WEIGHT')",
    )
    error_reason: str | None = Field(
        default=None,
        description="Stručný důvod chyby, pokud log_type='INVALID'",
    )

    @field_validator("weight_kg")
    @classmethod
    def validate_weight_limits(cls, value: float | None) -> float | None:
        """Validate body weight is within [30.0, 300.0] kg (Fail Fast)."""
        if value is not None and (value < MIN_WEIGHT_KG or value > MAX_WEIGHT_KG):
            raise WeightValidationError(value)
        return value

    @model_validator(mode="after")
    def validate_log_consistency(self) -> "UserLog":
        """Verify semantic consistency based on log_type."""
        if self.log_type == "WEIGHT":
            if self.weight_kg is None:
                raise ValueError("Pro záznam typu 'WEIGHT' musí být zadána hodnota 'weight_kg'.")
        elif self.log_type == "FOOD":
            if not self.items:
                raise ValueError(
                    "Pro záznam typu 'FOOD' musí seznam 'items' obsahovat alespoň jednu položku."
                )
        elif self.log_type == "INVALID" and not self.error_reason:
            self.error_reason = "Nespecifikovaná chyba vstupu."
        return self


class FoodDefinition(BaseModel):
    """Food entry in local foods.json database."""

    model_config = ConfigDict(extra="ignore")

    name: str
    calories: float
    protein: float
    diaas_coefficient: float = 0.70
    carbs: float
    fats: float


class CalculatedFoodLog(BaseModel):
    """Processed food log ready for SQLite persistence."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    logged_at: str = Field(default_factory=utc_now_iso)
    raw_input: str
    food_name_matched: str
    amount_g: float
    state: Literal["raw", "cooked"]
    is_estimated: bool
    calories: float
    protein_raw: float
    protein_usable: float
    carbs: float
    fats: float


class CalculatedWeightLog(BaseModel):
    """Processed weight log ready for SQLite persistence."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    logged_at: str = Field(default_factory=utc_now_iso)
    raw_input: str
    weight_kg: float
