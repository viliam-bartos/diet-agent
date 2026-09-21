"""Tests for the FastAPI web dashboard and its REST endpoints."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nutrition_agent.web as web_module
from nutrition_agent.db import get_connection, init_db
from nutrition_agent.models import CalculatedFoodLog, CalculatedWeightLog, CalculatedWorkoutLog
from nutrition_agent.repository import save_food_logs, save_weight_log, save_workout_log

# Sandbox restricts the OS temp directory, so the test database lives in a workspace folder.
# One reused file keeps the workspace clean; each test truncates the tables first.
TEST_DB_PATH = Path(__file__).resolve().parent.parent / ".tmp" / "tests" / "web_api_test.db"


def _reset_tables(db_path: Path) -> None:
    """Ensure the schema exists and remove all rows from previous tests."""
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        for table in ("food_logs", "weight_logs", "workout_logs", "user_settings"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def temp_db_path() -> Path:
    """Provide the shared workspace test database path."""
    TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return TEST_DB_PATH


@pytest.fixture
def api_client(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient backed by a temporary SQLite database with seeded data."""
    db_path = temp_db_path
    _reset_tables(db_path)

    conn = get_connection(db_path)

    today = datetime.now()
    for offset in range(5):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{day}T12:00:00Z",
                    raw_input="200g kuřecí prsa",
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
            ],
        )
        save_weight_log(
            conn,
            CalculatedWeightLog(
                logged_at=f"{day}T07:00:00Z",
                raw_input=f"{83.0 + offset * 0.2:.1f} kg",
                weight_kg=round(83.0 + offset * 0.2, 2),
            ),
        )
        if offset == 0:
            save_workout_log(
                conn,
                CalculatedWorkoutLog(
                    logged_at=f"{day}T18:00:00Z",
                    raw_input="trenink nohy 60 min",
                    workout_type="Silový trénink",
                    duration_minutes=60,
                ),
            )
    conn.commit()
    conn.close()

    settings_override = type(web_module.settings)(
        database_path=db_path,
        foods_data_path=web_module.settings.foods_data_path,
        log_level="WARNING",
        gemini_api_key=web_module.settings.gemini_api_key,
        discord_bot_token=None,
        web_host="127.0.0.1",
        web_port=8000,
        target_calories=3150.0,
        target_protein_g=155.0,
    )
    monkeypatch.setattr(web_module, "settings", settings_override)

    return TestClient(web_module.create_app())


def test_dashboard_html_served(api_client: TestClient):
    """Root route returns the HTML dashboard with the required components."""
    response = api_client.get("/")
    assert response.status_code == 200
    body = response.text

    # Palette and typography from the design spec
    assert "--bg-paper: #F4EFE6" in body
    assert "--valve-orange: #F05A22" in body
    assert "Barlow Semi Condensed" in body

    # Required dashboard components
    assert "chart.js" in body.lower()
    assert "kpiWeight" in body
    assert "heatmapGrid" in body
    assert "recordsBody" in body
    assert "weekBalance" in body
    assert "dayPanel" in body

    # Row actions for editing and deleting records
    assert "Akce" in body
    assert "openEditDialog" in body
    assert "deleteRecord" in body
    assert 'id="editDialog"' in body


def test_dashboard_reports_its_version_and_disables_caching(api_client: TestClient):
    """A stale server must be detectable, and the page must never be cached.

    Without this, a browser could pair freshly rendered markup with an older API,
    which showed up as empty carb and fat cards plus 'Not Found' on edits.
    """
    from nutrition_agent import __version__

    response = api_client.get("/")
    assert response.headers["cache-control"] == "no-store, must-revalidate"

    body = response.text
    assert "{{APP_VERSION}}" not in body
    assert f"'{__version__}'" in body
    assert 'id="staleBanner"' in body
    assert "describeApiError" in body

    assert api_client.get("/api/version").json() == {"version": __version__}


def test_day_payload_carries_version_and_every_card_field(api_client: TestClient):
    """The day response must satisfy every field the cards read."""
    from nutrition_agent import __version__

    data = api_client.get("/api/day").json()

    required = (
        "calories",
        "target_calories",
        "protein_usable",
        "target_protein",
        "carbs",
        "target_carbs",
        "fats",
        "target_fats",
        "day_type",
        "version",
    )
    for field in required:
        assert field in data, field

    assert data["version"] == __version__


def test_dashboard_heatmap_legend_has_colours(api_client: TestClient):
    """Legend swatches must actually get the level colours.

    The rules used to be scoped as '.heatmap-cell.level-green', so the legend
    swatches rendered as empty boxes while the labels still showed.
    """
    body = api_client.get("/").text

    for level in ("green", "blue", "amber", "empty"):
        assert f"legend-box level-{level}" in body
        # The colour rule must be standalone, not scoped to heatmap cells only
        assert f".heatmap-cell.level-{level} " not in body
        assert f".level-{level} {{" in body or f".level-{level} {{ " in body

    assert ".level-green { background: #38A169" in body
    assert ".level-blue { background: #3182CE" in body
    assert ".level-amber { background: #DD6B20" in body
    assert ".level-empty { background: #E6DFC5" in body


def test_dashboard_has_no_decorative_subtitles(api_client: TestClient):
    """Descriptive subtitles were removed from the section headers."""
    body = api_client.get("/").text

    assert "GitHub-style" not in body
    assert "DIAAS Usable Protein & Energy Telemetry" not in body
    assert "Body jednotlivých vážení" not in body
    assert 'class="brand-subtitle"' not in body


def test_api_day_endpoint_returns_today(api_client: TestClient):
    """The day endpoint reports today's consumption against today's target."""
    from nutrition_agent import __version__

    response = api_client.get("/api/day")
    assert response.status_code == 200
    data = response.json()

    today = datetime.now().strftime("%Y-%m-%d")
    assert data["date"] == today
    assert data["calories"] == 212.0
    assert data["protein_usable"] == 46.0
    assert data["fats"] == 2.4
    assert data["target_calories"] == 3150.0
    assert data["target_carbs"] == 430.0
    assert data["target_fats"] == 82.5
    assert data["goal"] == "lean_bulk"
    assert data["remaining_calories"] == 3150.0 - 212.0
    assert data["version"] == __version__
    assert len(data["items"]) == 1


def test_api_day_endpoint_accepts_a_date(api_client: TestClient):
    """Passing a date returns that day, empty when nothing was logged."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    data = api_client.get("/api/day", params={"date": yesterday}).json()

    assert data["date"] == yesterday
    assert data["calories"] == 212.0  # the fixture logs one meal per day for five days

    empty = api_client.get("/api/day", params={"date": "1990-01-01"}).json()
    assert empty["calories"] == 0.0
    assert empty["items"] == []


def test_api_day_endpoint_rejects_a_bad_date(api_client: TestClient):
    """An unsupported date format is reported, not silently ignored."""
    response = api_client.get("/api/day", params={"date": "21.9.2026"})
    assert response.status_code == 422


def test_api_week_endpoint_reports_the_balance(api_client: TestClient):
    """The week endpoint returns a 7-day window with a signed balance."""
    data = api_client.get("/api/week").json()

    assert len(data["days"]) == 7
    assert data["logged_days"] == 5
    assert data["workout_days"] == 1
    assert data["total_calories"] == 212.0 * 5
    assert data["calorie_balance"] == round(
        data["total_calories"] - data["total_target_calories"], 0
    )
    # 212 kcal a day is far below any target
    assert data["calorie_balance"] < 0


def test_dashboard_kpi_cards_show_macros_with_targets(api_client: TestClient):
    """The header cards are the four macros, each with a goal line."""
    body = api_client.get("/").text

    for label in ("Kalorie", "Bílkoviny", "Sacharidy", "Tuky"):
        assert f'<div class="kpi-label">{label}</div>' in body

    for element in (
        "kpiCals",
        "kpiProtein",
        "kpiCarbs",
        "kpiFats",
        "kpiCalsBar",
        "kpiProteinBar",
        "kpiCarbsBar",
        "kpiFatsBar",
        "kpiCalsTarget",
        "kpiProteinTarget",
        "kpiCarbsTarget",
        "kpiFatsTarget",
    ):
        assert f'id="{element}"' in body

    # The workout-days card is gone, the weight moved into the chart header
    assert "Odtrénované dny" not in body
    assert "kpiWorkouts" not in body
    assert 'id="kpiWeight"' in body
    assert 'id="kpiWeightDelta"' in body


def test_dashboard_has_weekly_balance_and_day_panel(api_client: TestClient):
    """The weekly indicator and the click-through day panel are present."""
    body = api_client.get("/").text

    assert 'id="weekBalance"' in body
    assert 'id="weekDays"' in body
    assert 'id="weekStats"' in body
    assert 'id="dayPanel"' in body
    assert 'id="dayPanelItems"' in body
    assert "selectDay" in body
    assert "loadWeek" in body


def test_dashboard_dropped_the_timeframe_switcher(api_client: TestClient):
    """The period toggles are gone; the chart and calendar are fixed to a year."""
    body = api_client.get("/").text

    assert "timeframe-bar" not in body
    assert "timeframe-btn" not in body
    assert "setTimeframe" not in body
    assert "currentTimeframe" not in body

    # Both long views are requested with a fixed one-year window
    assert "YEAR_DAYS = 365" in body
    assert "/api/weight-trend?days=" in body
    assert "/api/heatmap?days=" in body


def test_api_weight_trend_endpoint(api_client: TestClient):
    """Weight trend endpoint returns points plus headline numbers."""
    response = api_client.get("/api/weight-trend", params={"days": 30})
    assert response.status_code == 200
    data = response.json()

    assert data["days"] == 30
    assert data["count"] == 5
    assert data["current_weight"] == 83.0
    assert data["start_weight"] == 83.8
    assert data["delta_kg"] == -0.8

    points = data["points"]
    assert len(points) == 5
    for point in points:
        assert set(point.keys()) == {"date", "weight_kg", "moving_avg_7d"}
    assert all(p["date"] <= points[-1]["date"] for p in points)


def test_api_weight_trend_defaults_to_a_year(api_client: TestClient):
    """Without a window the chart asks for 365 days."""
    data = api_client.get("/api/weight-trend").json()
    assert data["days"] == 365


def test_api_heatmap_endpoint(api_client: TestClient):
    """Heatmap endpoint returns one cell per day with adherence levels."""
    response = api_client.get("/api/heatmap", params={"days": 7})
    assert response.status_code == 200
    cells = response.json()

    assert len(cells) == 7
    levels = {c["level"] for c in cells}
    assert levels <= {"green", "blue", "amber", "empty"}
    # Today has a workout logged
    today_key = datetime.now().strftime("%Y-%m-%d")
    today_cell = next(c for c in cells if c["date"] == today_key)
    assert today_cell["has_workout"] is True


def test_api_heatmap_defaults_to_a_year(api_client: TestClient):
    """The calendar covers a full year by default."""
    cells = api_client.get("/api/heatmap").json()
    assert len(cells) == 365
    assert cells[-1]["date"] == datetime.now().strftime("%Y-%m-%d")


def test_api_records_filtering_and_pagination(api_client: TestClient):
    """Records endpoint honours type filter, limit and offset."""
    response = api_client.get("/api/records", params={"type": "food", "limit": 2, "offset": 0})
    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 5
    assert data["limit"] == 2
    assert data["offset"] == 0
    assert len(data["items"]) == 2
    assert all(item["type"] == "food" for item in data["items"])

    second_page = api_client.get(
        "/api/records", params={"type": "food", "limit": 2, "offset": 2}
    ).json()
    assert len(second_page["items"]) == 2

    workouts = api_client.get("/api/records", params={"type": "workout"}).json()
    assert workouts["total"] == 1
    assert "Silový trénink" in workouts["items"][0]["title"]


def test_api_records_validation_rejects_bad_limit(api_client: TestClient):
    """Limit outside the allowed range is rejected by FastAPI validation."""
    assert api_client.get("/api/records", params={"limit": 0}).status_code == 422
    assert api_client.get("/api/records", params={"limit": 500}).status_code == 422


def test_api_message_command_without_llm(api_client: TestClient):
    """Local command through the message endpoint needs no LLM call."""
    response = api_client.post("/api/message", json={"message": "dnes"})
    assert response.status_code == 200
    reply = response.json()["reply"]
    assert "Denní souhrn" in reply or "nebyly nalezeny žádné záznamy" in reply


def test_api_message_empty_input(api_client: TestClient):
    """Empty message is rejected without reaching the LLM."""
    response = api_client.post("/api/message", json={"message": "   "})
    assert response.status_code == 200
    assert response.json()["reply"] == "Zadán prázdný vstup."


def test_api_message_requires_payload_field(api_client: TestClient):
    """Missing message field yields a validation error."""
    assert api_client.post("/api/message", json={}).status_code == 422


def test_api_message_accepts_a_target_date(api_client: TestClient, temp_db_path: Path):
    """The message endpoint can backdate a record to a chosen day."""
    target = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

    response = api_client.post(
        "/api/message",
        json={"message": "dnes", "date": target},
    )
    assert response.status_code == 200
    assert response.json()["date"] == target

    # An empty date means "today", which the endpoint reports as null
    plain = api_client.post("/api/message", json={"message": "dnes"})
    assert plain.json()["date"] is None

    # A malformed date is rejected before anything is written
    bad = api_client.post("/api/message", json={"message": "dnes", "date": "3.9.2026"})
    assert bad.status_code == 422


def test_create_app_upgrades_legacy_schema(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Starting the web app adds tables missing from older database files."""
    legacy_schema = """
    CREATE TABLE food_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        logged_at TEXT NOT NULL,
        raw_input TEXT NOT NULL,
        food_name_matched TEXT NOT NULL,
        amount_g REAL NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('raw', 'cooked')),
        is_estimated INTEGER NOT NULL CHECK (is_estimated IN (0, 1)),
        calories REAL NOT NULL,
        protein_raw REAL NOT NULL,
        protein_usable REAL NOT NULL,
        carbs REAL NOT NULL,
        fats REAL NOT NULL
    );
    """
    # Rebuild the file as a legacy database that only has food_logs
    legacy_conn = get_connection(temp_db_path)
    for table in ("food_logs", "weight_logs", "workout_logs", "user_settings"):
        legacy_conn.execute(f"DROP TABLE IF EXISTS {table}")
    legacy_conn.executescript(legacy_schema)
    legacy_conn.commit()
    legacy_conn.close()

    pre_conn = get_connection(temp_db_path)
    try:
        pre_tables = {
            row[0]
            for row in pre_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        pre_conn.close()
    assert "workout_logs" not in pre_tables

    settings_override = type(web_module.settings)(
        database_path=temp_db_path,
        foods_data_path=web_module.settings.foods_data_path,
        log_level="WARNING",
        gemini_api_key=None,
        discord_bot_token=None,
        web_host="127.0.0.1",
        web_port=8000,
        target_calories=None,
        target_protein_g=None,
    )
    monkeypatch.setattr(web_module, "settings", settings_override)

    client = TestClient(web_module.create_app())
    assert client.get("/api/day").status_code == 200

    conn = get_connection(temp_db_path)
    try:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()

    assert {"food_logs", "weight_logs", "workout_logs", "user_settings"} <= names


def test_create_app_uses_derived_targets_when_env_unset(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Without env targets the day endpoint falls back to weight-derived targets."""
    settings_override = type(web_module.settings)(
        database_path=web_module.settings.database_path,
        foods_data_path=web_module.settings.foods_data_path,
        log_level="WARNING",
        gemini_api_key=None,
        discord_bot_token=None,
        web_host="127.0.0.1",
        web_port=8000,
        target_calories=None,
        target_protein_g=None,
    )
    monkeypatch.setattr(web_module, "settings", settings_override)

    client = TestClient(web_module.create_app())
    data = client.get("/api/day").json()

    # Latest seeded weight is 83.0 kg -> lean bulk training day targets
    assert data["target_calories"] == 3150.0
    assert data["target_protein"] == 155.0
