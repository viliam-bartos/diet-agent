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
from nutrition_agent.models import CalculatedFoodLog, CalculatedWeightLog, CalculatedWorkoutLog
from nutrition_agent.reporting import format_balance_report
from nutrition_agent.repository import (
    get_calorie_offset,
    save_food_logs,
    save_weight_log,
    save_workout_log,
)


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


def test_maintenance_calibration_requires_five_weigh_ins_and_two_weeks():
    """Calibration requires at least 5 weigh-ins over >= 14 days and shifts all targets."""
    from nutrition_agent.targets import DayType, DietGoal, calculate_daily_targets

    conn_short = _seeded(days_logged=15, kcal=2900.0, weights=[82.0, 82.1, 82.2, 82.3])
    cal_short = build_maintenance_calibration(conn_short)
    assert cal_short["ready"] is False
    assert cal_short["weigh_ins_count"] == 4
    assert cal_short["required_weigh_ins"] == 5

    # 5 weigh-ins over only 5 consecutive days (< 14 days) is not ready either.
    conn_too_narrow = _seeded(days_logged=10, kcal=2975.0, weights=[83.0] * 5)
    cal_narrow = build_maintenance_calibration(conn_too_narrow)
    assert cal_narrow["ready"] is False
    assert cal_narrow["weigh_ins_count"] == 5
    assert cal_narrow["weigh_in_span_days"] == 5
    assert cal_narrow["min_span_days"] == 14

    # 15 weigh-ins across 15 days with flat scale weight (83.0 kg) while eating 2975 kcal/day
    # (+500 kcal/day above the 2475 kcal rest maintenance for 83 kg): calibration picks
    # strictly 5 weigh-ins spanning >= 14 days.
    conn = _seeded(days_logged=15, kcal=2975.0, weights=[83.0] * 15)
    cal = build_maintenance_calibration(conn)
    assert cal["ready"] is True
    assert cal["weigh_ins_count"] == 5
    assert cal["weigh_in_span_days"] >= 14
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
    maint = calculate_daily_targets(83.0, DietGoal.MAINTENANCE, DayType.REST, offset)
    bulk = calculate_daily_targets(83.0, DietGoal.LEAN_BULK, DayType.REST, offset)
    cut = calculate_daily_targets(83.0, DietGoal.CUT, DayType.REST, offset)
    assert maint.calories == 2975.0
    assert bulk.calories == 3350.0
    assert cut.calories == 2650.0


def test_maintenance_calibration_with_measured_gain_calculates_correct_maintenance():
    """A +1.2 kg gain across 5 weigh-ins over 30 days calibrates maintenance to ~2668 kcal/day (+80)."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    ref = datetime(2026, 11, 2)
    for i in range(30):
        d = (ref - timedelta(days=30 - i)).strftime("%Y-%m-%d")
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{d}T12:00:00+00:00",
                    raw_input="food",
                    food_name_matched="food",
                    amount_g=100.0,
                    state="raw",
                    is_estimated=False,
                    calories=2941.5,
                    protein_raw=150.0,
                    protein_usable=150.0,
                    carbs=300.0,
                    fats=80.0,
                )
            ],
        )
        # 12 training days so average baseline maintenance matches 77 644 / 30 = 2588 kcal/day
        if i % 5 in (0, 2):
            save_workout_log(
                conn,
                CalculatedWorkoutLog(
                    logged_at=f"{d}T18:00:00+00:00",
                    raw_input="trenink",
                    workout_type="Silový trénink",
                    duration_minutes=60,
                ),
            )

    # 5 weigh-ins evenly spaced across the 30-day window (days 0, 7, 14, 22, 29),
    # growing linearly from 81.9 kg to 83.1 kg (+1.2 kg over 30 days).
    for offset in (0, 7, 14, 22, 29):
        day_str = (ref - timedelta(days=30 - offset)).strftime("%Y-%m-%d")
        w = round(81.9 + 1.2 * (offset / 29.0), 4)
        save_weight_log(
            conn,
            CalculatedWeightLog(
                logged_at=f"{day_str}T07:00:00+00:00",
                raw_input=f"{w} kg",
                weight_kg=w,
            ),
        )

    verdict = build_balance_verdict(conn, days=30, ref_date=ref)
    assert verdict["maintenance_kcal"] == 77644.0
    cal = verdict["calibration"]
    assert cal["ready"] is True
    assert cal["weigh_ins_count"] == 5
    assert cal["weigh_in_span_days"] == 30
    assert cal["delta_kg"] == 1.2
    assert cal["current_maintenance_kcal"] == 2588.0
    assert cal["recommended_maintenance_kcal"] == 2668.0
    assert cal["recommended_delta_kcal"] == 80.0

    applied = apply_maintenance_calibration(conn, ref_date=ref)
    assert applied["applied"] is True
    assert applied["applied_delta_kcal"] == 80.0
    assert get_calorie_offset(conn) == 80.0


def test_maintenance_calibration_uses_ols_slope_across_five_weigh_ins():
    """Calibration uses OLS slope over strictly the last 5 weigh-ins (spanning >= 14 days)."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    ref = datetime(2026, 11, 2)
    for i in range(30):
        d = (ref - timedelta(days=30 - i)).strftime("%Y-%m-%d")
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{d}T12:00:00+00:00",
                    raw_input="food",
                    food_name_matched="food",
                    amount_g=100.0,
                    state="raw",
                    is_estimated=False,
                    calories=2941.5,
                    protein_raw=150.0,
                    protein_usable=150.0,
                    carbs=300.0,
                    fats=80.0,
                )
            ],
        )

    # Seed 2 older outlier weigh-ins and then 5 weigh-ins across 19 calendar days (offsets 0, 4, 9, 14, 18).
    save_weight_log(
        conn,
        CalculatedWeightLog(
            logged_at=f"{(ref - timedelta(days=30)).strftime('%Y-%m-%d')}T07:00:00+00:00",
            raw_input="75.0 kg",
            weight_kg=75.0,
        ),
    )
    offsets_from_day4 = [0, 4, 9, 14, 18]
    noise = [0.20, -0.36, 0.0, 0.36, -0.20]
    for offset, err in zip(offsets_from_day4, noise, strict=True):
        day_str = (ref - timedelta(days=27 - offset)).strftime("%Y-%m-%d")
        true_w = 82.0 + 0.04 * (offset * 19.0 / 18.0)
        save_weight_log(
            conn,
            CalculatedWeightLog(
                logged_at=f"{day_str}T07:00:00+00:00",
                raw_input="w",
                weight_kg=round(true_w + err, 4),
            ),
        )

    verdict = build_balance_verdict(conn, days=30, ref_date=ref)
    cal = verdict["calibration"]
    assert cal["ready"] is True
    assert cal["weigh_ins_count"] == 5
    assert cal["weigh_in_span_days"] == 19
    assert cal["delta_kg"] == 0.76
    assert cal["slope_kg_per_day"] == 0.04
    assert cal["slope_kg_per_week"] == 0.28


def test_balance_verdict_measures_gain_from_average_of_last_three_weigh_ins():
    """Actual mass change in a balance window compares start weight to average of last 3 weigh-ins."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    ref = datetime(2026, 11, 4)
    for i in range(1, 21):
        d = (ref - timedelta(days=i)).strftime("%Y-%m-%d")
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{d}T12:00:00+00:00",
                    raw_input="food",
                    food_name_matched="food",
                    amount_g=100.0,
                    state="raw",
                    is_estimated=False,
                    calories=3000.0,
                    protein_raw=160.0,
                    protein_usable=160.0,
                    carbs=300.0,
                    fats=80.0,
                )
            ],
        )

    # First weigh-in: 80.0 kg. Last 3 weigh-ins: 81.2, 81.5, 81.8 -> avg = 81.5 kg -> +1.50 kg.
    weigh_ins = [
        (20, 80.0),
        (14, 80.6),
        (7, 81.2),
        (4, 81.5),
        (1, 81.8),
    ]
    for days_ago, w in weigh_ins:
        d = (ref - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        save_weight_log(
            conn,
            CalculatedWeightLog(
                logged_at=f"{d}T07:00:00+00:00",
                raw_input=f"{w} kg",
                weight_kg=w,
            ),
        )

    verdict = build_balance_verdict(conn, days=30, ref_date=ref)
    assert verdict["first_weight_kg"] == 80.0
    assert verdict["last_weight_kg"] == 81.5
    assert verdict["end_weights_averaged"] == 3
    assert verdict["measured_kg"] == 1.5


def test_balance_active_period_stops_at_gap_longer_than_seven_days():
    """Active period includes consecutive logged days with gaps <= 7 days, and calibration is independent."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    ref = datetime(2026, 11, 4)
    # Earlier cluster: 40..35 days ago (followed by a 10-day gap > 7 days)
    for days_ago in range(35, 41):
        d = (ref - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{d}T12:00:00+00:00",
                    raw_input="old",
                    food_name_matched="old",
                    amount_g=100.0,
                    state="raw",
                    is_estimated=False,
                    calories=2800.0,
                    protein_raw=150.0,
                    protein_usable=150.0,
                    carbs=250.0,
                    fats=70.0,
                )
            ],
        )

    # Latest active cluster: 24 days ago, 18 days ago (gap = 6 days <= 7), and 15..1 days ago
    active_days_ago = [24, 18] + list(range(15, 0, -1))
    for days_ago in active_days_ago:
        d = (ref - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{d}T12:00:00+00:00",
                    raw_input="active",
                    food_name_matched="active",
                    amount_g=100.0,
                    state="raw",
                    is_estimated=False,
                    calories=3100.0,
                    protein_raw=160.0,
                    protein_usable=160.0,
                    carbs=300.0,
                    fats=80.0,
                )
            ],
        )

    for days_ago, w in [(24, 82.0), (18, 82.2), (12, 82.4), (6, 82.6), (1, 82.8)]:
        d = (ref - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        save_weight_log(
            conn,
            CalculatedWeightLog(
                logged_at=f"{d}T07:00:00+00:00",
                raw_input=f"{w} kg",
                weight_kg=w,
            ),
        )

    v_active = build_balance_verdict(conn, ref_date=ref, period="active")
    v_30d = build_balance_verdict(conn, days=30, ref_date=ref, period="30d")
    v_90d = build_balance_verdict(conn, days=90, ref_date=ref, period="90d")

    assert v_active["period"] == "active"
    assert v_active["start_date"] == (ref - timedelta(days=24)).strftime("%Y-%m-%d")
    assert v_active["end_date"] == (ref - timedelta(days=1)).strftime("%Y-%m-%d")
    assert v_active["logged_days"] == len(active_days_ago)

    assert v_90d["period"] == "90d"
    assert v_90d["window_days"] == 90
    assert v_90d["logged_days"] == len(active_days_ago) + 6

    # Calibration is identical across all three balance windows!
    assert v_active["calibration"] == v_30d["calibration"] == v_90d["calibration"]


def test_changing_calorie_target_preserves_previous_months_completed_days():
    """Setting a new calorie target today does not overwrite targets or green days from past months."""
    from nutrition_agent.cli import handle_user_message
    from nutrition_agent.repository import get_heatmap_data, set_calorie_target

    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    # Set initial weight 73.5 kg so LEAN_BULK REST target is ~2800 kcal (2799.2 kcal)
    save_weight_log(
        conn,
        CalculatedWeightLog(
            logged_at="2026-09-01T07:00:00+00:00",
            raw_input="73.5 kg",
            weight_kg=73.5,
        ),
    )
    set_calorie_target(conn, 2800.0, effective_date="2026-09-01")

    # Log 5 days in September 2026 with 2650 kcal (which is >= 90% of 2800 = 2520 -> GREEN,
    # but would be < 90% of 3000 = 2700 -> AMBER if overwritten!).
    sept_days = [f"2026-09-0{i}" for i in range(1, 6)]
    for day in sept_days:
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{day}T12:00:00+00:00",
                    raw_input="jídlo",
                    food_name_matched="Kuřecí prsa",
                    amount_g=200.0,
                    state="raw",
                    is_estimated=False,
                    calories=2650.0,
                    protein_raw=160.0,
                    protein_usable=160.0,
                    carbs=280.0,
                    fats=70.0,
                )
            ],
        )

    before = get_heatmap_data(conn, "2026-09-01T00:00:00", "2026-09-05T23:59:59")
    assert all(cell["level"] == "green" for cell in before)
    assert all(cell["target_calories"] == 2800.0 for cell in before)

    # Now two months later (2026-11-04), change the calorie target to 3000 kcal via CLI command!
    reply = handle_user_message("cil 3000", [], conn, target_day="2026-11-04")
    assert "3000 kcal" in reply

    # Today's target is 3000 kcal...
    nov = get_heatmap_data(conn, "2026-11-04T00:00:00", "2026-11-04T23:59:59")
    assert nov[0]["target_calories"] == 3000.0

    # ...while September's completed days still have target 2800 kcal and remain GREEN!
    after = get_heatmap_data(conn, "2026-09-01T00:00:00", "2026-09-05T23:59:59")
    assert all(cell["target_calories"] == 2800.0 for cell in after)
    assert all(cell["level"] == "green" for cell in after)


