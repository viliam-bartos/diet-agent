"""Tests for Discord progress bars, weight deltas, trend, heatmap and dashboard aggregations."""

import sqlite3
from datetime import datetime, timedelta

import discord
import pytest

from nutrition_agent.bot import render_discord_progress_bar
from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import CalculatedFoodLog, CalculatedWeightLog, CalculatedWorkoutLog
from nutrition_agent.repository import (
    get_daily_weight_comparison,
    get_day_detail,
    get_day_summaries,
    get_heatmap_data,
    get_unified_records,
    get_week_summary,
    get_weight_delta,
    get_weight_trend,
    get_weight_trend_for_days,
    get_workout_logs_in_range,
    save_food_logs,
    save_weight_log,
    save_workout_log,
)


@pytest.fixture
def seeded_db() -> sqlite3.Connection:
    """Fixture with 10 days of food, weight and workout logs."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    base_day = datetime(2026, 9, 8)

    for offset in range(10):
        day = (base_day + timedelta(days=offset)).strftime("%Y-%m-%d")

        # Food: 2000 kcal, 140 g DIAAS protein per day
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{day}T12:00:00Z",
                    raw_input="test",
                    food_name_matched="Kuřecí prsa",
                    amount_g=200.0,
                    state="raw",
                    is_estimated=False,
                    calories=2000.0,
                    protein_raw=140.0,
                    protein_usable=140.0,
                    carbs=150.0,
                    fats=60.0,
                )
            ],
        )

        # Weight: linear growth from 83.0 kg
        save_weight_log(
            conn,
            CalculatedWeightLog(
                logged_at=f"{day}T07:00:00Z",
                raw_input=f"{83.0 + offset * 0.1:.1f} kg",
                weight_kg=round(83.0 + offset * 0.1, 2),
            ),
        )

        # Workout every third day
        if offset % 3 == 0:
            save_workout_log(
                conn,
                CalculatedWorkoutLog(
                    logged_at=f"{day}T18:00:00Z",
                    raw_input="trenink nohy 60 min",
                    workout_type="Silový trénink",
                    duration_minutes=60,
                ),
            )

    return conn


def test_discord_progress_bar_rendering():
    """Verify Unicode block progress bar output and percentage clamping."""
    assert render_discord_progress_bar(50.0, 100.0, width=10) == "[█████░░░░░] 50 %"
    assert render_discord_progress_bar(0.0, 100.0, width=10) == "[░░░░░░░░░░] 0 %"
    assert render_discord_progress_bar(100.0, 100.0, width=10) == "[██████████] 100 %"

    # Over-target values are clamped to a full bar but keep the real percentage
    over = render_discord_progress_bar(200.0, 100.0, width=10)
    assert over.startswith("[██████████]")
    assert "200 %" in over

    # Zero or negative target must not raise
    assert render_discord_progress_bar(10.0, 0.0, width=10) == "[░░░░░░░░░░] 0 %"


def test_weight_delta_over_period(seeded_db: sqlite3.Connection):
    """Verify weight delta computation across an explicit window."""
    start = "2026-09-08T00:00:00"
    end = "2026-09-17T23:59:59"

    stats = get_weight_delta(seeded_db, start, end)
    assert stats["count"] == 10
    assert stats["start_weight"] == 83.0
    assert stats["current_weight"] == 83.9
    assert stats["delta_kg"] == 0.9
    assert stats["min_weight"] == 83.0
    assert stats["max_weight"] == 83.9


def test_weight_delta_empty_period_uses_latest(seeded_db: sqlite3.Connection):
    """Empty window falls back to the latest known weight with zero delta."""
    stats = get_weight_delta(seeded_db, "2000-01-01T00:00:00", "2000-01-02T00:00:00")
    assert stats["count"] == 0
    assert stats["delta_kg"] == 0.0
    assert stats["current_weight"] == 83.9


def test_daily_weight_comparison_delta(seeded_db: sqlite3.Connection):
    """Verify today vs previous logged day delta."""
    current, delta = get_daily_weight_comparison(seeded_db, "2026-09-17")
    assert current is not None
    assert current.weight_kg == 83.9
    assert delta == 0.1


def test_daily_weight_comparison_without_previous_log():
    """Day with a single weight log has no delta."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    save_weight_log(
        conn,
        CalculatedWeightLog(logged_at="2026-09-17T07:00:00Z", raw_input="83 kg", weight_kg=83.0),
    )

    current, delta = get_daily_weight_comparison(conn, "2026-09-17")
    assert current is not None
    assert delta is None


def test_daily_weight_comparison_missing_day(seeded_db: sqlite3.Connection):
    """Day without any weight log returns None for both values."""
    current, delta = get_daily_weight_comparison(seeded_db, "2030-01-01")
    assert current is None
    assert delta is None


def test_weight_trend_moving_average(seeded_db: sqlite3.Connection):
    """Verify 7-day rolling average smoothing the raw weight points."""
    trend = get_weight_trend(seeded_db, "2026-09-08T00:00:00", "2026-09-17T23:59:59")

    assert len(trend) == 10
    assert trend[0]["date"] == "2026-09-08"
    assert trend[0]["weight_kg"] == 83.0
    # First point has only itself in the window
    assert trend[0]["moving_avg_7d"] == 83.0

    # 7th point averages the first seven days: 83.0 through 83.6 -> 83.3
    assert trend[6]["date"] == "2026-09-14"
    assert trend[6]["moving_avg_7d"] == 83.3

    # 8th point window is days 2-8 (83.1 .. 83.7) -> 83.4
    assert trend[7]["moving_avg_7d"] == 83.4

    # Last point window is days 4-10 (83.3 .. 83.9) -> 83.6
    assert trend[9]["moving_avg_7d"] == 83.6


def test_weight_trend_empty(seeded_db: sqlite3.Connection):
    """Empty window yields an empty trend list."""
    assert get_weight_trend(seeded_db, "2000-01-01T00:00:00", "2000-01-02T00:00:00") == []


def test_day_summaries_and_week_balance(seeded_db: sqlite3.Connection):
    """Per-day summaries feed the weekly balance."""
    summaries = get_day_summaries(seeded_db, "2026-09-08", "2026-09-17")

    assert len(summaries) == 10
    assert all(day["calories"] == 2000.0 for day in summaries)
    assert all(day["protein_usable"] == 140.0 for day in summaries)

    week = get_week_summary(seeded_db, "2026-09-17", days=7)
    assert week["start_date"] == "2026-09-11"
    assert week["end_date"] == "2026-09-17"
    assert week["logged_days"] == 7
    assert week["total_calories"] == 14000.0
    # Workouts fall on 09-08, 09-11, 09-14 and 09-17, so three land in this window
    assert week["workout_days"] == 3
    assert week["calorie_balance"] == round(14000.0 - week["total_target_calories"], 0)
    # 2000 kcal a day against ~2700-3200 kcal targets is a deficit
    assert week["calorie_balance"] < 0
    assert week["protein_balance"] < 0


def test_week_summary_on_empty_database():
    """An empty database yields zeroed week figures instead of an error."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    week = get_week_summary(conn, "2026-09-17", days=7)
    assert week["logged_days"] == 0
    assert week["total_calories"] == 0.0
    assert week["calorie_balance"] == 0.0
    assert week["avg_calories"] == 0.0
    assert len(week["days"]) == 7


def test_day_detail_includes_totals_targets_and_items(seeded_db: sqlite3.Connection):
    """A single day reports consumption, its target, and the logged items."""
    detail = get_day_detail(seeded_db, "2026-09-08")

    assert detail["date"] == "2026-09-08"
    assert detail["calories"] == 2000.0
    assert detail["protein_usable"] == 140.0
    assert detail["has_workout"] is True
    assert detail["day_type"] == "training"
    assert detail["remaining_calories"] == round(detail["target_calories"] - 2000.0, 0)
    assert len(detail["items"]) == 1
    assert detail["items"][0]["name"] == "Kuřecí prsa"
    assert detail["weight_kg"] == 83.0


def test_day_detail_on_day_without_records(seeded_db: sqlite3.Connection):
    """A day with nothing logged reports zeroes and no items."""
    detail = get_day_detail(seeded_db, "2030-01-01")

    assert detail["calories"] == 0.0
    assert detail["items"] == []
    assert detail["has_food"] is False
    assert detail["weight_kg"] is None


def test_weight_trend_for_days_returns_headline_numbers(seeded_db: sqlite3.Connection):
    """The chart endpoint returns points plus the numbers shown next to it."""
    data = get_weight_trend_for_days(seeded_db, days=30)

    assert data["count"] == 10
    assert data["points"]
    # The seeded window ends today, so old logs fall outside it unless recent
    assert "start_date" in data and "end_date" in data


def test_weight_trend_for_days_covers_a_full_year():
    """The default year-long window starts 364 days before today."""
    from datetime import datetime, timedelta

    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    save_weight_log(
        conn,
        CalculatedWeightLog(
            logged_at=datetime.now().astimezone().isoformat(),
            raw_input="83 kg",
            weight_kg=83.0,
        ),
    )

    data = get_weight_trend_for_days(conn, days=365)
    assert data["days"] == 365
    assert data["end_date"] == datetime.now().strftime("%Y-%m-%d")
    assert data["start_date"] == (datetime.now() - timedelta(days=364)).strftime("%Y-%m-%d")
    assert data["current_weight"] == 83.0
    assert data["delta_kg"] == 0.0


def test_heatmap_levels_and_workout_markers(seeded_db: sqlite3.Connection):
    """Verify heatmap adherence levels, workout markers and day coverage.

    Each day is graded against its own target: the seeded rest days use the rest
    target and the training days use the training target, so the same intake can
    land in different levels.
    """
    cells = get_heatmap_data(seeded_db, "2026-09-08T00:00:00", "2026-09-17T23:59:59")

    assert len(cells) == 10
    assert [c["date"] for c in cells] == sorted(c["date"] for c in cells)

    assert cells[0]["has_workout"] is True
    assert cells[1]["has_workout"] is False
    assert cells[0]["workout_type"] == "Silový trénink"
    assert cells[0]["calories"] == 2000.0
    assert cells[0]["protein_usable"] == 140.0

    # 2026-09-08 has a workout, so it is judged against the current training target
    from nutrition_agent.targets import DayType, DietGoal, calculate_daily_targets

    latest_weight = 83.9  # newest weight log in the seeded fixture
    training = calculate_daily_targets(latest_weight, DietGoal.LEAN_BULK, DayType.TRAINING)
    rest = calculate_daily_targets(latest_weight, DietGoal.LEAN_BULK, DayType.REST)

    assert cells[0]["day_type"] == "training"
    assert cells[0]["target_calories"] == training.calories
    # 2026-09-09 has no workout, so it uses the rest target
    assert cells[1]["day_type"] == "rest"
    assert cells[1]["target_calories"] == rest.calories
    assert rest.calories < training.calories

    # 2000 kcal and 140 g DIAAS is under both targets. Protein only clears the rest
    # target, so training days grade amber while rest days grade blue.
    for cell in cells:
        if cell["has_workout"]:
            assert cell["level"] == "amber", cell["date"]
        else:
            assert cell["level"] == "blue", cell["date"]


def test_heatmap_uses_per_day_targets(seeded_db: sqlite3.Connection):
    """A rest day is not punished for missing the higher training target."""
    cells = get_heatmap_data(seeded_db, "2026-09-08T00:00:00", "2026-09-17T23:59:59")
    by_date = {c["date"]: c for c in cells}

    training_day = by_date["2026-09-08"]
    rest_day = by_date["2026-09-09"]

    assert training_day["has_workout"] is True
    assert rest_day["has_workout"] is False
    assert rest_day["target_calories"] < training_day["target_calories"]
    assert rest_day["target_protein"] < training_day["target_protein"]


def test_heatmap_level_classification():
    """Verify blue (protein only), amber (under target) and empty (no data) levels."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    def add_day(day: str, calories: float, protein: float) -> None:
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{day}T12:00:00Z",
                    raw_input="test",
                    food_name_matched="Test",
                    amount_g=100.0,
                    state="raw",
                    is_estimated=False,
                    calories=calories,
                    protein_raw=protein,
                    protein_usable=protein,
                    carbs=0.0,
                    fats=0.0,
                )
            ],
        )

    # Protein target hit, calories far below -> blue
    add_day("2026-09-01", 1000.0, 150.0)
    # Both below target -> amber
    add_day("2026-09-02", 1000.0, 50.0)
    # No record -> empty
    cells = get_heatmap_data(conn, "2026-09-01T00:00:00", "2026-09-03T23:59:59")

    levels = {c["date"]: c["level"] for c in cells}
    assert levels["2026-09-01"] == "blue"
    assert levels["2026-09-02"] == "amber"
    assert levels["2026-09-03"] == "empty"


def test_heatmap_without_explicit_range_spans_a_full_year(seeded_db: sqlite3.Connection):
    """Without a range the calendar covers the last 365 days ending today."""
    from datetime import datetime, timedelta

    cells = get_heatmap_data(seeded_db)

    assert len(cells) == 365
    today = datetime.now().strftime("%Y-%m-%d")
    expected_start = (datetime.now() - timedelta(days=364)).strftime("%Y-%m-%d")
    assert cells[0]["date"] == expected_start
    assert cells[-1]["date"] == today


def test_unified_records_pagination_and_filtering(seeded_db: sqlite3.Connection):
    """Verify unified history ordering, filtering and pagination counters."""
    all_items, total = get_unified_records(seeded_db, record_type="all", limit=5, offset=0)
    assert total == 24  # 10 food + 10 weight + 4 workout
    assert len(all_items) == 5
    # Sorted descending by timestamp
    assert all_items[0]["logged_at"] >= all_items[-1]["logged_at"]

    next_page, _ = get_unified_records(seeded_db, record_type="all", limit=5, offset=5)
    assert len(next_page) == 5
    assert {i["logged_at"] for i in next_page}.isdisjoint({i["logged_at"] for i in all_items})

    food_items, food_total = get_unified_records(seeded_db, record_type="food")
    assert food_total == 10
    assert all(i["type"] == "food" for i in food_items)

    weight_items, weight_total = get_unified_records(seeded_db, record_type="weight")
    assert weight_total == 10
    assert all(i["type"] == "weight" for i in weight_items)

    workout_items, workout_total = get_unified_records(seeded_db, record_type="workout")
    assert workout_total == 4
    assert all(i["type"] == "workout" for i in workout_items)
    assert all("Silový trénink" in i["title"] for i in workout_items)


def test_workout_logs_in_range_filtering(seeded_db: sqlite3.Connection):
    """Verify workout range filtering boundaries."""
    logs = get_workout_logs_in_range(seeded_db, "2026-09-08T00:00:00", "2026-09-14T23:59:59")
    # Offsets 0, 3, 6 fall inside 2026-09-08 .. 2026-09-14
    assert len(logs) == 3
    assert all(log.duration_minutes == 60 for log in logs)


def test_workout_log_roundtrip_preserves_none_duration():
    """Workout without duration stores and reads back NULL correctly."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    saved = save_workout_log(
        conn,
        CalculatedWorkoutLog(raw_input="trenink", workout_type="Běh", duration_minutes=None),
    )
    assert saved.id is not None

    logs = get_workout_logs_in_range(conn)
    assert len(logs) == 1
    assert logs[0].duration_minutes is None
    assert logs[0].workout_type == "Běh"


def test_bot_registers_expected_slash_commands(monkeypatch: pytest.MonkeyPatch):
    """Verify the bot exposes the required slash commands and dashboard button."""
    import nutrition_agent.bot as bot_module

    client, tree = bot_module.create_bot()
    command_names = {cmd.name for cmd in tree.get_commands()}

    assert {"dnes", "vaha", "trenink", "prehled", "web"} <= command_names

    view = bot_module.build_dashboard_view()
    assert len(view.children) == 1
    button = view.children[0]
    assert isinstance(button, discord.ui.Button)
    assert button.style == discord.ButtonStyle.link
    assert button.url.startswith("http://")


def test_bot_message_content_intent_is_enabled(monkeypatch: pytest.MonkeyPatch):
    """The bot must subscribe to message content to read natural language logs."""
    import nutrition_agent.bot as bot_module

    client, _ = bot_module.create_bot()
    assert client.intents.message_content is True


def test_application_id_decoded_from_token():
    """The bot application ID is decoded from the first token segment."""
    import base64

    import nutrition_agent.bot as bot_module

    app_id = 1550206419297636394
    encoded = base64.b64encode(str(app_id).encode()).decode().rstrip("=")
    token = f"{encoded}.abcdef.ghijklmnopqrstuvwxyz0123456789"

    assert bot_module.application_id_from_token(token) == app_id


def test_application_id_returns_none_for_garbage():
    """Malformed tokens do not raise, they return None."""
    import nutrition_agent.bot as bot_module

    assert bot_module.application_id_from_token("") is None
    assert bot_module.application_id_from_token("not-a-token") is None


def test_build_invite_url_requests_bot_and_command_scopes():
    """The invite URL must request both bot and applications.commands scopes."""
    import nutrition_agent.bot as bot_module

    url = bot_module.build_invite_url(123456789)
    assert url.startswith("https://discord.com/oauth2/authorize?")
    assert "client_id=123456789" in url
    assert "applications.commands" in url
    assert "scope=bot" in url


def test_guild_id_from_env(monkeypatch: pytest.MonkeyPatch):
    """DISCORD_GUILD_ID is parsed into an int for instant command sync."""
    from nutrition_agent.config import Settings

    monkeypatch.setenv("DISCORD_GUILD_ID", "987654321098765432")
    parsed = Settings.load()
    assert parsed.discord_guild_id == 987654321098765432

    monkeypatch.setenv("DISCORD_GUILD_ID", "")
    assert Settings.load().discord_guild_id is None


def test_bot_requires_token_fail_fast(monkeypatch: pytest.MonkeyPatch):
    """Running the bot without a token raises a clear configuration error."""
    import asyncio

    import nutrition_agent.bot as bot_module

    settings_override = type(bot_module.settings)(
        database_path=bot_module.settings.database_path,
        foods_data_path=bot_module.settings.foods_data_path,
        log_level="WARNING",
        gemini_api_key=None,
        discord_bot_token=None,
        discord_guild_id=None,
        web_host="127.0.0.1",
        web_port=8000,
        target_calories=None,
        target_protein_g=None,
    )
    monkeypatch.setattr(bot_module, "settings", settings_override)

    with pytest.raises(ValueError, match="DISCORD_BOT_TOKEN"):
        asyncio.run(bot_module.run_bot_async())
