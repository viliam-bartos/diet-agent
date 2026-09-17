"""Tests for dietary goal targets, day types, and reporting aggregations."""

import sqlite3
from datetime import datetime

import pytest

from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import CalculatedFoodLog, CalculatedWeightLog
from nutrition_agent.reporting import (
    generate_period_report,
    get_period_boundaries,
    render_bar_chart,
    render_progress_bar,
)
from nutrition_agent.repository import (
    get_user_profile,
    save_food_logs,
    save_weight_log,
    set_user_setting,
)
from nutrition_agent.targets import (
    DayType,
    DietGoal,
    calculate_daily_targets,
    parse_day_type,
    parse_goal,
)


def test_calculate_daily_targets_83kg_lean_bulk():
    """Verify target calculation matches spec for 83 kg in Lean Bulk."""
    # Training day: ~3150 kcal, 155g P, 82.5g F, 430g C
    t_train = calculate_daily_targets(83.0, DietGoal.LEAN_BULK, DayType.TRAINING)
    assert t_train.calories == 3150.0
    assert t_train.protein_usable == 155.0
    assert t_train.fats == 82.5
    assert t_train.carbs == 430.0

    # Rest day: ~2650 kcal, 145g P, 77.5g F, 340g C
    t_rest = calculate_daily_targets(83.0, DietGoal.LEAN_BULK, DayType.REST)
    assert t_rest.calories == 2650.0
    assert t_rest.protein_usable == 145.0
    assert t_rest.fats == 77.5
    assert t_rest.carbs == 340.0


def test_calculate_daily_targets_maintenance_and_cut():
    """Verify targets for Maintenance and Cut."""
    m_train = calculate_daily_targets(83.0, DietGoal.MAINTENANCE, DayType.TRAINING)
    assert m_train.calories == 2750.0
    assert m_train.protein_usable == 150.0

    c_train = calculate_daily_targets(83.0, DietGoal.CUT, DayType.TRAINING)
    assert c_train.calories == 2200.0
    assert c_train.protein_usable == 175.0
    assert c_train.fats == 68.0


def test_calculate_daily_targets_dynamic_weight_scaling():
    """Verify targets dynamically scale when body weight changes."""
    t_80 = calculate_daily_targets(80.0, DietGoal.LEAN_BULK, DayType.TRAINING)
    t_90 = calculate_daily_targets(90.0, DietGoal.LEAN_BULK, DayType.TRAINING)

    assert t_80.calories < 3150.0
    assert t_90.calories > 3150.0
    assert t_90.protein_usable > t_80.protein_usable

    with pytest.raises(ValueError, match="Hmotnost musí být kladné číslo"):
        calculate_daily_targets(-5.0)


def test_parse_goal_and_day_type():
    """Verify parsing strings to DietGoal and DayType enums."""
    assert parse_goal("bulk") == DietGoal.LEAN_BULK
    assert parse_goal("nabirani") == DietGoal.LEAN_BULK
    assert parse_goal("maintenance") == DietGoal.MAINTENANCE
    assert parse_goal("udrzba") == DietGoal.MAINTENANCE
    assert parse_goal("cut") == DietGoal.CUT
    assert parse_goal("rysovani") == DietGoal.CUT

    with pytest.raises(ValueError, match="Neznámý cíl"):
        parse_goal("invalid_goal")

    assert parse_day_type("trenink") == DayType.TRAINING
    assert parse_day_type("odpocinek") == DayType.REST
    assert parse_day_type("rest") == DayType.REST

    with pytest.raises(ValueError, match="Neznámý typ dne"):
        parse_day_type("invalid_day")


def test_render_progress_bar():
    """Verify text progress bar formatting."""
    bar_50 = render_progress_bar(50.0, 100.0, width=10)
    assert "[#####-----]" in bar_50
    assert "50.0%" in bar_50

    bar_zero = render_progress_bar(10.0, 0.0, width=10)
    assert "[----------]" in bar_zero


def test_render_bar_chart():
    """Verify horizontal bar chart rendering."""
    items = [
        ("2026-09-01", 1000.0, "kcal"),
        ("2026-09-02", 2000.0, "kcal"),
    ]
    lines = render_bar_chart(items, width=10)
    assert len(lines) == 2
    assert "2026-09-01" in lines[0]
    assert "2026-09-02" in lines[1]
    assert "2000.0 kcal" in lines[1]


def test_get_period_boundaries():
    """Verify timeframe boundary resolution."""
    ref = datetime(2026, 9, 20)
    start_d, end_d, title_d = get_period_boundaries("denni", ref)
    assert start_d == "2026-09-20"
    assert end_d == "2026-09-20"

    start_w, end_w, _ = get_period_boundaries("tydenni", ref)
    assert start_w == "2026-09-14"
    assert end_w == "2026-09-20"

    start_all, end_all, _ = get_period_boundaries("kompletni", ref)
    assert start_all is None
    assert end_all is None

    with pytest.raises(ValueError, match="Neznámé období"):
        get_period_boundaries("sto_let")


@pytest.fixture
def populated_db() -> sqlite3.Connection:
    """Fixture providing populated test database."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    set_user_setting(conn, "goal", DietGoal.LEAN_BULK.value)
    set_user_setting(conn, "day_type", DayType.TRAINING.value)

    # 3 days of food logs
    days = ["2026-09-15", "2026-09-16", "2026-09-17"]
    for d in days:
        food = CalculatedFoodLog(
            logged_at=f"{d}T12:30:00Z",
            raw_input="200g kuřecí",
            food_name_matched="Kuřecí prsa",
            amount_g=200.0,
            state="raw",
            is_estimated=False,
            calories=212.0,
            protein_raw=46.0,
            protein_usable=46.0,
            carbs=0.0,
            fats=2.4,
        )
        save_food_logs(conn, [food])

    # 2 weight logs
    w1 = CalculatedWeightLog(logged_at="2026-09-15T08:00:00Z", raw_input="82.5", weight_kg=82.5)
    w2 = CalculatedWeightLog(logged_at="2026-09-17T08:00:00Z", raw_input="83.2", weight_kg=83.2)
    save_weight_log(conn, w1)
    save_weight_log(conn, w2)
    return conn


def test_generate_period_report_weekly(populated_db: sqlite3.Connection):
    """Verify weekly report calculates daily averages and weight delta."""
    ref = datetime(2026, 9, 17)
    report = generate_period_report(populated_db, "tydenni", ref_date=ref)

    assert "Týdenní souhrn" in report
    assert "Počet dnů se záznamem stravy: 3"
    assert "Celkem přijato: 636 kcal"
    assert "Průměrné denní hodnoty vs cíl:"
    assert "Graf přijaté energie po dnech:"
    assert "Statistika tělesné hmotnosti:"
    assert "Počáteční váha: 82.5 kg"
    assert "Poslední váha:   83.2 kg"
    assert "Rozdíl za období: +0.7 kg"


def test_generate_period_report_all(populated_db: sqlite3.Connection):
    """Verify complete report spans all data."""
    report = generate_period_report(populated_db, "kompletni")
    assert "Kompletní souhrn (všechna data)" in report
    assert "Počet dnů se záznamem stravy: 3"
    assert "83.2 kg" in report


def test_user_profile_automatic_weight_sync(populated_db: sqlite3.Connection):
    """Verify get_user_profile automatically picks up latest weight."""
    goal, day_type, weight = get_user_profile(populated_db)
    assert goal == DietGoal.LEAN_BULK
    assert day_type == DayType.TRAINING
    assert weight == 83.2
