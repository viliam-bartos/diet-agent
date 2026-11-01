"""Tests for the maintenance balance and the body mass it explains."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from nutrition_agent.balance import (
    FAT_KCAL_PER_KG,
    LEAN_KCAL_PER_KG,
    LEAN_SHARE_OF_GAIN,
    MAX_LEAN_GAIN_KG_PER_30_DAYS,
    apply_maintenance_calibration,
    build_balance_verdict,
    build_maintenance_calibration,
    energy_per_kg_of_gain,
    estimate_mass_change,
)
from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import CalculatedFoodLog, CalculatedWeightLog
from nutrition_agent.reporting import format_balance_report
from nutrition_agent.repository import get_calorie_offset, save_food_logs, save_weight_log


def test_energy_per_kg_of_gain_mixes_lean_and_fat():
    """A kilo of mixed tissue costs less than a kilo of fat."""
    expected = LEAN_SHARE_OF_GAIN * LEAN_KCAL_PER_KG + (1 - LEAN_SHARE_OF_GAIN) * FAT_KCAL_PER_KG
    assert energy_per_kg_of_gain() == round(expected, 1)
    assert energy_per_kg_of_gain() == 5600.0
    assert energy_per_kg_of_gain() < FAT_KCAL_PER_KG
    assert energy_per_kg_of_gain() > LEAN_KCAL_PER_KG


def test_estimate_mass_change_splits_a_surplus():
    """A surplus up to the monthly muscle cap becomes 50:50 mixed tissue, a deficit mostly fat."""
    estimate = estimate_mass_change(5600.0, days=30)
    assert estimate.kilograms == 1.0
    assert estimate.lean_kilograms == 0.5
    assert estimate.fat_kilograms == 0.5
    assert estimate.gain is True

    # Above the 0.50 kg/30d muscle cap, excess surplus goes into fat at 7700 kcal/kg.
    large_surplus = estimate_mass_change(11472.0, days=30)
    assert large_surplus.lean_kilograms == MAX_LEAN_GAIN_KG_PER_30_DAYS
    assert large_surplus.fat_kilograms == 1.26
    assert large_surplus.kilograms == 1.76

    loss = estimate_mass_change(-FAT_KCAL_PER_KG)
    assert loss.kilograms == -1.0
    assert loss.fat_kilograms == -1.0
    assert loss.lean_kilograms == 0.0
    assert loss.gain is False


def _day(offset_from_today: int) -> str:
    return (datetime.now() - timedelta(days=offset_from_today)).strftime("%Y-%m-%d")


def _seeded(days_logged: int, kcal: float, weights: list[float]) -> sqlite3.Connection:
    """Log `days_logged` finished days with the same intake and a rising scale."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    for index in range(days_logged):
        day = _day(index + 1)  # finished days only
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{day}T12:00:00+00:00",
                    raw_input="test",
                    food_name_matched="Test",
                    amount_g=100.0,
                    state="raw",
                    is_estimated=False,
                    calories=kcal,
                    protein_raw=150.0,
                    protein_usable=150.0,
                    carbs=200.0,
                    fats=80.0,
                )
            ],
        )

    for index, weight in enumerate(weights):
        # Oldest first: the first entry has to sit earliest in the window.
        offset = len(weights) - index
        save_weight_log(
            conn,
            CalculatedWeightLog(
                logged_at=f"{_day(offset)}T07:00:00+00:00",
                raw_input=f"{weight} kg",
                weight_kg=weight,
            ),
        )

    return conn


def test_balance_verdict_compares_with_maintenance():
    """The window ends yesterday and only logged days enter the comparison."""
    from nutrition_agent.targets import DayType, DietGoal, calculate_daily_targets

    conn = _seeded(days_logged=10, kcal=3000.0, weights=[83.0, 84.0])
    verdict = build_balance_verdict(conn, days=30)

    assert verdict["end_date"] == _day(1)
    assert verdict["window_days"] == 30
    assert verdict["logged_days"] == 10
    assert verdict["intake_kcal"] == 30000.0

    # Maintenance is computed from the current weight, and the seeded days are rest days.
    daily_maintenance = calculate_daily_targets(
        verdict["weight_kg"], DietGoal.MAINTENANCE, DayType.REST
    ).calories
    assert verdict["maintenance_kcal"] == round(daily_maintenance * 10, 0)
    assert verdict["balance_kcal"] == round(30000.0 - daily_maintenance * 10, 0)
    assert verdict["surplus"] is (verdict["balance_kcal"] >= 0)

    expected_estimate = estimate_mass_change(verdict["balance_kcal"], days=10)
    assert verdict["estimated_kg"] == expected_estimate.kilograms
    assert verdict["measured_kg"] == 1.0
    assert verdict["deviation_kg"] == round(1.0 - expected_estimate.kilograms, 2)
    assert verdict["deviation_percent"] is not None


def test_balance_verdict_reports_a_deficit():
    """Eating below maintenance shows a negative balance, not a surplus."""
    conn = _seeded(days_logged=10, kcal=2000.0, weights=[84.0, 83.0])
    verdict = build_balance_verdict(conn, days=30)

    assert verdict["balance_kcal"] < 0
    assert verdict["surplus"] is False
    assert verdict["estimated_kg"] < 0
    assert verdict["measured_kg"] == -1.0


def test_balance_verdict_without_weights_leaves_the_deviation_unknown():
    """A comparison against the scale needs two weigh-ins; without them it is unknown."""
    conn = _seeded(days_logged=10, kcal=3000.0, weights=[83.5])

    verdict = build_balance_verdict(conn, days=30)

    assert verdict["measured_kg"] is None
    assert verdict["deviation_kg"] is None
    assert verdict["deviation_percent"] is None
    assert any("dvě vážení" in warning for warning in verdict["warnings"])


def test_balance_verdict_warns_about_missing_days():
    """A month with a handful of logged days must not look like a measured balance."""
    conn = _seeded(days_logged=3, kcal=3000.0, weights=[])

    verdict = build_balance_verdict(conn, days=30)

    assert verdict["logged_days"] == 3
    assert verdict["logged_share"] < 0.70
    assert any("orientační" in warning for warning in verdict["warnings"])


def test_balance_verdict_rejects_a_zero_window():
    """The window has to be at least one day (Fail Fast)."""
    conn = _seeded(days_logged=1, kcal=2000.0, weights=[])

    with pytest.raises(ValueError, match="kladné číslo"):
        build_balance_verdict(conn, days=0)


def test_balance_report_is_honest_about_the_model():
    """The formatted report states the assumptions and the deviation."""
    conn = _seeded(days_logged=10, kcal=3000.0, weights=[83.0, 84.0])
    report = format_balance_report(build_balance_verdict(conn, days=30))

    assert "Bilance proti maintenance" in report
    assert "Zaznamenaných dní: 10 z 30" in report
    assert "Bilance:" in report and "přebytek" in report
    assert "Odhad hmoty:" in report
    assert "Skutečná změna:" in report
    assert "Odchylka:" in report
    assert "Je to odhad, ne měření" in report


def test_maintenance_calibration_requires_seven_weigh_ins_and_shifts_targets():
    """Calibration waits for 7 weigh-ins, computes the new baseline, and shifts all targets."""
    from nutrition_agent.targets import DayType, DietGoal, calculate_daily_targets

    conn_short = _seeded(days_logged=10, kcal=2900.0, weights=[82.0, 82.0, 82.0])
    cal_short = build_maintenance_calibration(conn_short)
    assert cal_short["ready"] is False
    assert cal_short["weigh_ins_count"] == 3
    assert cal_short["required_weigh_ins"] == 7

    # 7 weigh-ins across 7 days with flat scale weight (83.0 kg) while eating 2975 kcal/day
    # (+500 kcal/day above the 2475 kcal rest maintenance for 83 kg).
    conn = _seeded(days_logged=7, kcal=2975.0, weights=[83.0] * 7)
    cal = build_maintenance_calibration(conn)
    assert cal["ready"] is True
    assert cal["weigh_ins_count"] == 7
    assert cal["delta_kg"] == 0.0
    assert cal["current_maintenance_kcal"] == 2475.0
    assert cal["recommended_delta_kcal"] == 500.0
    assert cal["recommended_maintenance_kcal"] == 2975.0

    applied = apply_maintenance_calibration(conn)
    assert applied["applied"] is True
    assert applied["applied_delta_kcal"] == 500.0
    assert get_calorie_offset(conn) == 500.0
    assert applied["calibration"]["recommended_delta_kcal"] == 0.0

    # All derived targets (MAINTENANCE, LEAN_BULK, CUT) shift by the calibrated offset.
    offset = get_calorie_offset(conn)
    assert calculate_daily_targets(83.0, DietGoal.MAINTENANCE, DayType.REST, offset).calories == 2975.0
    assert calculate_daily_targets(83.0, DietGoal.LEAN_BULK, DayType.REST, offset).calories == 3350.0
    assert calculate_daily_targets(83.0, DietGoal.CUT, DayType.REST, offset).calories == 2650.0
