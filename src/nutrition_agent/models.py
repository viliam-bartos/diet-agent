"""Data models, schemas, and custom exceptions for Nutrition Agent."""

import contextlib
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Domain constants
MIN_WEIGHT_KG: float = 30.0
MAX_WEIGHT_KG: float = 300.0
MIN_FOOD_MATCH_SCORE: float = 75.0
MAX_UNIT_GRAMS: float = 5000.0


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def parse_date_argument(value: str | None) -> str:
    """Resolve an optional day argument into a 'YYYY-MM-DD' string.

    Accepts 'YYYY-MM-DD', the words 'dnes'/'today' and 'vcera'/'yesterday'.
    An empty or missing value means today.
    """
    if value is None or not value.strip():
        return datetime.now().strftime("%Y-%m-%d")

    cleaned = value.strip().lower()
    if cleaned in ("dnes", "dnesek", "today"):
        return datetime.now().strftime("%Y-%m-%d")
    if cleaned in ("vcera", "včera", "yesterday"):
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"Neplatné datum '{value}'. Použij formát RRRR-MM-DD, 'dnes' nebo 'vcera'."
        ) from exc


def food_name_key(name: str) -> str:
    """Normalized lookup key for a food name.

    SQLite's built-in NOCASE collation only folds ASCII, so 'Kuřecí prsa' would
    never match 'KUŘECÍ PRSA'. Python's lower() handles Unicode, so the key is
    computed here and stored next to the display name.
    """
    return " ".join(name.split()).lower()


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


# Units the parser may return. Absolute units carry their own amount, the rest are
# converted to grams by the local code (curated table, learned table, or an estimate).
LOG_UNITS: tuple[str, ...] = (
    "g",
    "ml",
    "ks",
    "porce",
    "odměrka",
    "lžíce",
    "lžička",
    "hrnek",
    "plátek",
    "krajíc",
)


class ExtractedFoodItem(BaseModel):
    """Food item extracted by LLM parser."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(
        ...,
        description="Standardizovaný název potraviny v češtině, např. 'kuřecí prsa', 'rýže'",
    )
    amount: float = Field(
        ...,
        description="Numerické množství suroviny v gramech, ml nebo kusech",
    )
    unit: Literal[
        "g",
        "ml",
        "ks",
        "porce",
        "odměrka",
        "lžíce",
        "lžička",
        "hrnek",
        "plátek",
        "krajíc",
    ] = Field(
        default="g",
        description=(
            "Jednotka množství. Použij 'g' nebo 'ml' pro vážené suroviny a konkrétní "
            "míru ('odměrka', 'lžíce', 'lžička', 'hrnek', 'plátek', 'krajíc', 'ks', "
            "'porce'), pokud ji uživatel uvedl."
        ),
    )
    state: Literal["raw", "cooked"] = Field(
        default="raw",
        description="Stav potraviny: 'raw' (syrový stav) nebo 'cooked' (tepelně upravené jídlo)",
    )
    is_estimated: bool = Field(
        default=False,
        description="True, pokud gramáž nebyla v textu explicitně uvedena číslem",
    )

    @field_validator("amount")
    @classmethod
    def validate_positive_amount(cls, value: float) -> float:
        """Validate amount is strictly positive (Fail Fast)."""
        if value <= 0:
            raise ValueError("Množství suroviny musí být kladné číslo větší než 0.")
        return value


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

    @model_validator(mode="before")
    @classmethod
    def normalize_llm_payload(cls, data: object) -> object:
        """Normalize raw dictionary from LLM before strict validation."""
        if not isinstance(data, dict):
            return data

        # Normalize log_type if present
        log_type = data.get("log_type")
        if isinstance(log_type, str):
            data["log_type"] = log_type.upper().strip()

        if data.get("log_type") == "WEIGHT":
            # If weight_kg is missing, inspect alternative field names
            if data.get("weight_kg") is None:
                for alt_key in ("weight", "body_weight", "value", "vaha", "hodnota"):
                    if alt_key in data and data[alt_key] is not None:
                        try:
                            data["weight_kg"] = float(str(data[alt_key]).replace(",", "."))
                            break
                        except (ValueError, TypeError):
                            pass

            # If still missing, check if model erroneously put the weight into items
            if data.get("weight_kg") is None and data.get("items"):
                first_item = data["items"][0]
                if isinstance(first_item, dict) and first_item.get("amount"):
                    with contextlib.suppress(ValueError, TypeError):
                        data["weight_kg"] = float(first_item["amount"])

            # In WEIGHT logs, items must always be empty
            data["items"] = []

        return data

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
    """Food entry with nutrition values per 100 g.

    ``source`` distinguishes curated catalog entries from nutrition values that
    were estimated by the LLM for a food missing from the catalog.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    calories: float
    protein: float
    diaas_coefficient: float = 0.70
    carbs: float
    fats: float
    source: Literal["curated", "ai"] = "curated"


class EstimatedFoodNutrition(BaseModel):
    """Nutrition values estimated by the LLM for an unknown food, per 100 g."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(description="Normalizovaný název potraviny v češtině, jednotné číslo")
    calories: float = Field(description="Energie na 100 g v kcal")
    protein: float = Field(description="Bílkoviny na 100 g v gramech")
    carbs: float = Field(description="Sacharidy na 100 g v gramech")
    fats: float = Field(description="Tuky na 100 g v gramech")
    protein_category: Literal[
        "animal",
        "plant_isolate",
        "legumes",
        "grains_seeds",
        "nuts_peanuts",
        "unknown",
    ] = Field(
        default="unknown",
        description="Kategorie zdroje bílkovin pro DIAAS diskontování",
    )

    @field_validator("calories", "protein", "carbs", "fats")
    @classmethod
    def validate_non_negative(cls, value: float) -> float:
        """Nutrition values cannot be negative (Fail Fast)."""
        if value < 0:
            raise ValueError("Nutriční hodnota nesmí být záporná.")
        return value

    @model_validator(mode="after")
    def validate_energy_consistency(self) -> "EstimatedFoodNutrition":
        """Reject estimates whose macros cannot add up to the stated energy."""
        macro_energy = self.protein * 4.0 + self.carbs * 4.0 + self.fats * 9.0
        if self.calories > 0 and macro_energy > 0:
            deviation = abs(macro_energy - self.calories) / self.calories
            if deviation > 0.35:
                raise ValueError(
                    f"Odhad je nekonzistentní: makra dávají {macro_energy:.0f} kcal, "
                    f"ale uvedeno je {self.calories:.0f} kcal."
                )
        return self


class EstimatedUnitConversion(BaseModel):
    """Grams per one unit of a food, as estimated by the LLM.

    Zero is a valid answer meaning 'this unit is unknown for this food'. The caller
    decides whether to refuse the log; the schema only rejects nonsense.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(description="Název potraviny, ke které se převod vztahuje")
    unit: str = Field(description="Jednotka, např. 'odměrka', 'lžíce', 'porce', 'ks'")
    grams_per_unit: float = Field(
        default=0.0,
        description="Kolik gramů odpovídá jedné jednotce; 0 znamená, že jednotku neznáš",
    )

    @field_validator("grams_per_unit")
    @classmethod
    def validate_grams(cls, value: float) -> float:
        """Grams per unit must not be negative and must stay plausible (Fail Fast)."""
        if value < 0:
            raise ValueError("Hmotnost jedné jednotky nesmí být záporná.")
        if value > MAX_UNIT_GRAMS:
            raise ValueError(
                f"Hmotnost jedné jednotky {value} g je mimo rozumný rozsah "
                f"(maximum {MAX_UNIT_GRAMS:.0f} g)."
            )
        return value


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
    nutrition_source: Literal["curated", "ai"] = "curated"
    conversion_estimated: bool = False


class CalculatedWeightLog(BaseModel):
    """Processed weight log ready for SQLite persistence."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    logged_at: str = Field(default_factory=utc_now_iso)
    raw_input: str
    weight_kg: float


class CalculatedWorkoutLog(BaseModel):
    """Processed workout log ready for SQLite persistence."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    logged_at: str = Field(default_factory=utc_now_iso)
    raw_input: str
    workout_type: str = "Silový trénink"
    duration_minutes: int | None = None
    notes: str | None = None
