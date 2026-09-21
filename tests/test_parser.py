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


def test_local_date_converts_utc_to_the_local_day():
    """A timestamp just after local midnight must not fall on the previous day.

    Timestamps are stored in UTC. Without conversion, a meal eaten at 00:30 local
    time in a UTC+2 zone would be filed under the previous date.
    """
    from datetime import UTC, datetime, timedelta

    from nutrition_agent.models import local_date

    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    local_midnight = datetime(2026, 9, 18, 0, 30, tzinfo=datetime.now().astimezone().tzinfo)
    stored = local_midnight.astimezone(UTC).isoformat()

    assert local_date(stored) == "2026-09-18"
    # Sanity: the raw UTC prefix would differ whenever the offset is positive
    if offset.total_seconds() > 0:
        assert stored[:10] == "2026-09-17"


def test_local_date_handles_naive_and_malformed_input():
    """Naive timestamps are treated as UTC and junk falls back to the raw prefix."""
    from nutrition_agent.models import local_date

    assert local_date("2026-09-18T12:00:00") == "2026-09-18"
    assert local_date("nesmysl") == "nesmysl"


def test_local_timestamp_on_lands_on_the_requested_local_day():
    """Backdated records keep a plausible clock time but the chosen date."""
    from datetime import datetime

    from nutrition_agent.models import local_date, local_timestamp_on

    stamp = local_timestamp_on("2026-08-01")
    assert local_date(stamp) == "2026-08-01"

    # The stored value is UTC, so the local wall-clock time only matches after
    # converting back. That is the whole point of the helper.
    reference = datetime(2026, 8, 1, 14, 25, 30)
    explicit = local_timestamp_on("2026-08-01", moment=reference)
    assert local_date(explicit) == "2026-08-01"

    back_to_local = datetime.fromisoformat(explicit).astimezone()
    assert (back_to_local.hour, back_to_local.minute) == (14, 25)


def test_day_scoped_queries_use_the_local_day():
    """A late-night log is found on its local day, not the previous UTC date."""
    from datetime import UTC, datetime

    from nutrition_agent.db import SCHEMA_SQL, get_connection
    from nutrition_agent.models import CalculatedFoodLog, local_date
    from nutrition_agent.repository import get_daily_food_logs, save_food_logs

    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    tz = datetime.now().astimezone().tzinfo
    late_night = datetime(2026, 9, 18, 0, 30, tzinfo=tz)
    stored = late_night.astimezone(UTC).isoformat()

    save_food_logs(
        conn,
        [
            CalculatedFoodLog(
                logged_at=stored,
                raw_input="půlnoční svačina",
                food_name_matched="Kuřecí prsa",
                amount_g=100.0,
                state="raw",
                is_estimated=False,
                calories=106.0,
                protein_raw=23.0,
                protein_usable=23.0,
                carbs=0.0,
                fats=1.2,
            )
        ],
    )

    day = local_date(stored)
    assert len(get_daily_food_logs(conn, day)) == 1
