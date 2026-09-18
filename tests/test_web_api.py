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
    assert "timeframe-btn" in body
    assert "kpiWeight" in body
    assert "heatmapGrid" in body
    assert "recordsBody" in body
    assert "7 dní" in body
    assert "30 dní" in body
    assert "3 měsíce" in body
    assert "6 měsíců" in body
    assert "1 rok" in body
    assert "Vše" in body

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


def test_kpis_payload_carries_version_and_every_card_field(api_client: TestClient):
    """The KPI response must satisfy every field the cards read."""
    from nutrition_agent import __version__

    data = api_client.get("/api/kpis", params={"period": "30d"}).json()

    required = (
        "avg_calories",
        "target_calories",
        "avg_protein_diaas",
        "target_protein",
        "avg_carbs",
        "target_carbs",
        "avg_fats",
        "target_fats",
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


def test_api_kpis_endpoint(api_client: TestClient):
    """KPI endpoint returns weights, averages and configured targets."""
    response = api_client.get("/api/kpis", params={"period": "30d"})
    assert response.status_code == 200
    data = response.json()

    assert data["current_weight"] == 83.0
    assert data["weight_delta"] == -0.8
    assert data["avg_calories"] == 212.0
    assert data["avg_protein_diaas"] == 46.0
    assert data["avg_carbs"] == 0.0
    assert data["avg_fats"] == 2.4
    assert data["total_workout_days"] == 1
    assert data["total_food_days"] == 5
    assert data["target_calories"] == 3150.0
    assert data["target_protein"] == 155.0
    assert data["target_carbs"] == 430.0
    assert data["target_fats"] == 82.5
    assert data["goal"] == "lean_bulk"
    assert data["day_type"] == "training"


def test_api_kpis_averages_macros_per_logged_day(api_client: TestClient):
    """Carb and fat averages are per logged day, like calories and protein."""
    data = api_client.get("/api/kpis", params={"period": "30d"}).json()

    # The fixture logs one identical meal on each of five days
    assert data["avg_carbs"] == 0.0
    assert data["avg_fats"] == 2.4
    assert data["total_food_days"] == 5


def test_dashboard_kpi_cards_show_macros_with_targets(api_client: TestClient):
    """The header cards are the four macros, each with a goal line."""
    body = api_client.get("/").text

    for label in ("Kalorie / den", "Bílkoviny / den", "Sacharidy / den", "Tuky / den"):
        assert label in body

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


def test_api_weight_trend_endpoint(api_client: TestClient):
    """Weight trend endpoint returns points with a 7-day moving average."""
    response = api_client.get("/api/weight-trend", params={"period": "30d"})
    assert response.status_code == 200
    points = response.json()

    assert len(points) == 5
    for point in points:
        assert set(point.keys()) == {"date", "weight_kg", "moving_avg_7d"}
    assert all(p["date"] <= points[-1]["date"] for p in points)


def test_api_heatmap_endpoint(api_client: TestClient):
    """Heatmap endpoint returns one cell per day with adherence levels."""
    response = api_client.get("/api/heatmap", params={"period": "7d"})
    assert response.status_code == 200
    cells = response.json()

    assert len(cells) == 7
    levels = {c["level"] for c in cells}
    assert levels <= {"green", "blue", "amber", "empty"}
    # Today has a workout logged
    today_key = datetime.now().strftime("%Y-%m-%d")
    today_cell = next(c for c in cells if c["date"] == today_key)
    assert today_cell["has_workout"] is True


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


def test_resolve_period_dates_windows():
    """Period shorthands map onto expected rolling windows."""
    start_all, end_all = web_module._resolve_period_dates("all")
    assert start_all is None and end_all is None

    start_7, end_7 = web_module._resolve_period_dates("7d")
    assert end_7 is not None and start_7 is not None
    span_7 = datetime.strptime(end_7[:10], "%Y-%m-%d") - datetime.strptime(start_7[:10], "%Y-%m-%d")
    assert span_7.days == 6

    start_365, end_365 = web_module._resolve_period_dates("365d")
    span_365 = datetime.strptime(end_365[:10], "%Y-%m-%d") - datetime.strptime(
        start_365[:10], "%Y-%m-%d"
    )
    assert span_365.days == 364

    # Unknown period falls back to the 30-day default
    start_unknown, end_unknown = web_module._resolve_period_dates("nesmysl")
    span_unknown = datetime.strptime(end_unknown[:10], "%Y-%m-%d") - datetime.strptime(
        start_unknown[:10], "%Y-%m-%d"
    )
    assert span_unknown.days == 29


def test_api_kpis_applies_time_window(api_client: TestClient):
    """A narrow window excludes older records from the aggregation."""
    narrow = api_client.get("/api/kpis", params={"period": "1d"}).json()
    wide = api_client.get("/api/kpis", params={"period": "30d"}).json()

    assert wide["total_food_days"] == 5
    assert narrow["total_food_days"] <= wide["total_food_days"]


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
    assert client.get("/api/kpis", params={"period": "all"}).status_code == 200

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
    """Without env targets the KPI endpoint falls back to weight-derived targets."""
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
    data = client.get("/api/kpis", params={"period": "30d"}).json()

    # Latest seeded weight is 83.0 kg -> lean bulk training day targets
    assert data["target_calories"] == 3150.0
    assert data["target_protein"] == 155.0
