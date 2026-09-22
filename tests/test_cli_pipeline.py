"""Integration tests for CLI message handling and calculation pipeline."""

import sqlite3

import pytest

from nutrition_agent.cli import handle_user_message
from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import ExtractedFoodItem, FoodDefinition, UserLog
from nutrition_agent.repository import (
    get_user_profile,
    get_user_setting,
    get_workout_logs_in_range,
    set_user_setting,
)
from nutrition_agent.targets import DayType


@pytest.fixture
def test_setup() -> tuple[sqlite3.Connection, list[FoodDefinition]]:
    """Fixture providing in-memory DB and mock food catalog."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    foods = [
        FoodDefinition(
            name="Kuřecí prsa",
            calories=106.0,
            protein=23.0,
            diaas_coefficient=1.00,
            carbs=0.0,
            fats=1.2,
        ),
        FoodDefinition(
            name="Ovesné vločky",
            calories=370.0,
            protein=13.5,
            diaas_coefficient=0.70,
            carbs=58.5,
            fats=7.0,
        ),
    ]
    return conn, foods


def test_handle_empty_input(test_setup: tuple[sqlite3.Connection, list[FoodDefinition]]) -> None:
    """Empty string returns immediate message without calling parser."""
    conn, foods = test_setup
    result = handle_user_message("   ", foods, conn)
    assert result == "Zadán prázdný vstup."


def test_handle_invalid_log(test_setup: tuple[sqlite3.Connection, list[FoodDefinition]]) -> None:
    """INVALID log_type returns formatted error reason."""
    conn, foods = test_setup

    def mock_parser(msg: str) -> UserLog:
        return UserLog(log_type="INVALID", error_reason="Nespecifikované množství suroviny")

    result = handle_user_message("měl jsem rýži", foods, conn, parse_fn=mock_parser)
    assert "Chyba vstupu: Nespecifikované množství suroviny" in result


def test_handle_weight_log(test_setup: tuple[sqlite3.Connection, list[FoodDefinition]]) -> None:
    """Valid WEIGHT log saves to DB and returns confirmation."""
    conn, foods = test_setup

    def mock_parser(msg: str) -> UserLog:
        return UserLog(log_type="WEIGHT", weight_kg=82.5)

    result = handle_user_message("Váha 82.5 kg", foods, conn, parse_fn=mock_parser)
    assert "Zaznamenána tělesná hmotnost: 82.5 kg" in result

    cursor = conn.cursor()
    cursor.execute("SELECT weight_kg FROM weight_logs")
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 82.5


def test_handle_food_log_diaas_calculation(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
) -> None:
    """Valid FOOD log calculates calories, usable protein, and saves to DB."""
    conn, foods = test_setup

    def mock_parser(msg: str) -> UserLog:
        return UserLog(
            log_type="FOOD",
            items=[
                ExtractedFoodItem(
                    name="ovesné vločky",
                    amount=100.0,
                    unit="g",
                    state="raw",
                    is_estimated=False,
                )
            ],
        )

    result = handle_user_message("100g vloček", foods, conn, parse_fn=mock_parser)
    assert "Zaznamenáno jídlo:" in result
    assert "Ovesné vločky" in result
    assert f"{9.45:.1f} g DIAAS" in result
    assert "celkem 13.5 g" in result

    cursor = conn.cursor()
    cursor.execute("SELECT calories, protein_raw, protein_usable FROM food_logs")
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0]["calories"] == 370.0
    assert rows[0]["protein_raw"] == 13.5
    assert rows[0]["protein_usable"] == 9.45


def test_handle_unknown_food_learns_from_estimate(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A food missing from the catalog is estimated by the model and stored."""
    import nutrition_agent.cli as cli_module
    from nutrition_agent.models import EstimatedFoodNutrition
    from nutrition_agent.repository import get_food

    conn, foods = test_setup

    def fake_estimate(name: str, **kwargs) -> EstimatedFoodNutrition:
        assert name == "kuřecí kari"
        return EstimatedFoodNutrition(
            name="kuřecí kari",
            calories=130.0,
            protein=12.0,
            carbs=6.0,
            fats=6.0,
            protein_category="animal",
        )

    monkeypatch.setattr(cli_module, "estimate_food_nutrition", fake_estimate)

    def mock_parser(msg: str) -> UserLog:
        return UserLog(
            log_type="FOOD",
            items=[
                ExtractedFoodItem(
                    name="kuřecí kari",
                    amount=250.0,
                    unit="g",
                    state="cooked",
                    is_estimated=False,
                )
            ],
        )

    result = handle_user_message("250g kuřecího kari", foods, conn, parse_fn=mock_parser)

    assert "AI odhad maker" in result
    assert "nebyly v katalogu" in result
    assert "kuřecí kari" in result

    # The food is now part of the catalog, flagged as an AI estimate
    stored = get_food(conn, "kuřecí kari")
    assert stored is not None
    assert stored.source == "ai"
    assert stored.calories == 130.0
    assert stored.diaas_coefficient == 1.00  # animal category

    # And the log records where its numbers came from
    cursor = conn.cursor()
    cursor.execute("SELECT nutrition_source, food_name_matched FROM food_logs")
    row = cursor.fetchone()
    assert row["nutrition_source"] == "ai"
    assert row["food_name_matched"] == "kuřecí kari"


def test_learned_food_is_reused_without_asking_model_again(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once learned, the food matches locally and the model is not called again."""
    import nutrition_agent.cli as cli_module
    from nutrition_agent.models import EstimatedFoodNutrition
    from nutrition_agent.repository import load_foods

    conn, foods = test_setup
    calls: list[str] = []

    def fake_estimate(name: str, **kwargs) -> EstimatedFoodNutrition:
        calls.append(name)
        return EstimatedFoodNutrition(
            name="quinoa",
            calories=368.0,
            protein=14.0,
            carbs=64.0,
            fats=6.0,
            protein_category="grains_seeds",
        )

    monkeypatch.setattr(cli_module, "estimate_food_nutrition", fake_estimate)

    def mock_parser(msg: str) -> UserLog:
        return UserLog(
            log_type="FOOD",
            items=[
                ExtractedFoodItem(name="quinoa", amount=100.0, unit="g", state="raw"),
            ],
        )

    handle_user_message("100g quinoa", foods, conn, parse_fn=mock_parser)
    assert calls == ["quinoa"]

    # Second log against a freshly loaded catalog must not hit the model again
    reloaded = load_foods(conn)
    handle_user_message("100g quinoa", reloaded, conn, parse_fn=mock_parser)
    assert calls == ["quinoa"]

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM food_logs")
    assert cursor.fetchone()[0] == 2


def test_curated_catalog_wins_over_ai_estimate(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
) -> None:
    """A known curated food never reaches the estimator."""
    from nutrition_agent.cli import resolve_food

    conn, foods = test_setup
    food_def, was_learned = resolve_food(conn, "kuřecí prsa", foods)

    assert was_learned is False
    assert food_def.source == "curated"
    assert food_def.name == "Kuřecí prsa"


def test_estimate_of_a_known_food_is_not_stored_as_a_duplicate(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's canonical name is matched against the catalog before it is learned.

    'eidam 30' misses the catalog entry, the model answers with 'eidam 30%', which is
    that entry. Storing the estimate would create a second, differently valued row.
    """
    import nutrition_agent.cli as cli_module
    from nutrition_agent.cli import resolve_food
    from nutrition_agent.models import EstimatedFoodNutrition
    from nutrition_agent.repository import get_food

    conn, foods = test_setup
    foods.append(
        FoodDefinition(
            name="Eidam 30% tuku v sušině",
            calories=273.0,
            protein=30.0,
            diaas_coefficient=1.0,
            carbs=1.5,
            fats=16.7,
        )
    )

    def fake_estimate(name: str, **kwargs) -> EstimatedFoodNutrition:
        return EstimatedFoodNutrition(
            name="eidam 30%",
            calories=255.0,
            protein=29.0,
            carbs=1.0,
            fats=15.0,
            protein_category="animal",
        )

    monkeypatch.setattr(cli_module, "estimate_food_nutrition", fake_estimate)

    food_def, was_learned = resolve_food(conn, "eidam 30", foods)

    assert was_learned is False
    assert food_def.name == "Eidam 30% tuku v sušině"
    assert food_def.calories == 273.0

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM foods WHERE source = 'ai'")
    assert cursor.fetchone()[0] == 0
    assert get_food(conn, "eidam 30%") is None


def test_estimator_failure_is_reported_and_saves_nothing(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the model cannot estimate, the message fails without writing data."""
    import nutrition_agent.cli as cli_module

    conn, foods = test_setup

    def failing_estimate(name: str, **kwargs):
        raise RuntimeError("Model nevrátil odhad.")

    monkeypatch.setattr(cli_module, "estimate_food_nutrition", failing_estimate)

    def mock_parser(msg: str) -> UserLog:
        return UserLog(
            log_type="FOOD",
            items=[ExtractedFoodItem(name="záhadná věc", amount=100.0, unit="g", state="raw")],
        )

    result = handle_user_message("100g záhadná věc", foods, conn, parse_fn=mock_parser)
    assert result.startswith("Chyba:")

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM food_logs")
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT COUNT(*) FROM foods")
    assert cursor.fetchone()[0] == 0

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM food_logs")
    assert cursor.fetchone()[0] == 0


def test_handle_command_dnes_and_prehled(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
) -> None:
    """Keyword 'dnes' or 'prehled' outputs daily summary without invoking parser."""
    conn, foods = test_setup

    # Empty day first
    result_empty = handle_user_message("dnes", foods, conn)
    assert "Denní souhrn" in result_empty
    assert "Za vybrané období nebyly nalezeny žádné záznamy" in result_empty

    # Log an item
    def mock_parser(msg: str) -> UserLog:
        return UserLog(
            log_type="FOOD",
            items=[
                ExtractedFoodItem(
                    name="kuřecí prsa",
                    amount=200.0,
                    unit="g",
                    state="raw",
                )
            ],
        )

    handle_user_message("200g kuřecí prsa", foods, conn, parse_fn=mock_parser)

    result_after = handle_user_message("prehled", foods, conn)
    assert "Denní souhrn" in result_after
    assert "Kuřecí prsa" in result_after
    assert "Přehled plnění denního cíle:" in result_after
    assert "Energie:" in result_after


def test_handle_command_historie(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
) -> None:
    """Keyword 'historie' returns list of recent food and weight logs."""
    conn, foods = test_setup

    result_empty = handle_user_message("historie", foods, conn)
    assert "Historie je prázdná" in result_empty

    def mock_weight(msg: str) -> UserLog:
        return UserLog(log_type="WEIGHT", weight_kg=83.0)

    handle_user_message("83 kg", foods, conn, parse_fn=mock_weight)

    result_after = handle_user_message("history", foods, conn)
    assert "Historie záznamů:" in result_after
    assert "83.0 kg" in result_after


def test_handle_workout_command_with_duration(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
) -> None:
    """'trenink' command logs a workout and switches the day to TRAINING."""
    conn, foods = test_setup

    # Start from a rest day so the automatic switch is observable
    set_user_setting(conn, "day_type", DayType.REST.value)

    result = handle_user_message("trenink nohy 60 min", foods, conn)
    assert "Zaznamenán trénink: nohy" in result
    assert "60 min" in result
    assert "TRAINING" in result

    logs = get_workout_logs_in_range(conn)
    assert len(logs) == 1
    assert logs[0].workout_type == "nohy"
    assert logs[0].duration_minutes == 60
    assert logs[0].raw_input == "trenink nohy 60 min"

    assert get_user_setting(conn, "day_type") == DayType.TRAINING.value


def test_handle_workout_command_without_duration(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
) -> None:
    """'trenink' without a duration stores a NULL duration and a default type."""
    conn, foods = test_setup

    result = handle_user_message("trenink", foods, conn)
    assert "Zaznamenán trénink" in result

    logs = get_workout_logs_in_range(conn)
    assert len(logs) == 1
    assert logs[0].duration_minutes is None
    assert logs[0].workout_type == "Silový trénink"


def test_handle_goal_and_day_type_switching(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
) -> None:
    """'cil' and 'den' commands persist the profile and recompute targets."""
    conn, foods = test_setup

    result_goal = handle_user_message("cil cut", foods, conn)
    assert "CUT" in result_goal
    assert "kcal" in result_goal

    result_day = handle_user_message("den odpocinek", foods, conn)
    assert "REST" in result_day

    goal, day_type, weight = get_user_profile(conn)
    assert goal.value == "cut"
    assert day_type.value == "rest"
    assert weight == 83.0

    # Invalid values return the parser error string instead of raising
    assert "Neznámý cíl" in handle_user_message("cil nesmysl", foods, conn)
    assert "Neznámý typ dne" in handle_user_message("den nesmysl", foods, conn)


def test_handle_weight_log_recomputes_targets(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
) -> None:
    """Logging a new weight reports automatically recomputed targets."""
    conn, foods = test_setup

    def mock_weight(msg: str) -> UserLog:
        return UserLog(log_type="WEIGHT", weight_kg=90.0)

    result = handle_user_message("90 kg", foods, conn, parse_fn=mock_weight)
    assert "Zaznamenána tělesná hmotnost: 90.0 kg" in result
    assert "přepočteny" in result

    # Both meals and the daily report must now use the 90 kg targets
    report = handle_user_message("dnes", foods, conn)
    assert "90.0 kg" in report
    assert "3415" in report  # 90 kg * (3150 / 83) rounded


def _capture_service_call(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict[str, object]:
    """Run cli.main() with argv and capture how the service runner was invoked."""
    import nutrition_agent.cli as cli_module

    captured: dict[str, object] = {}

    def fake_run_all_services(*, include_bot: bool = True, open_browser: bool = False) -> None:
        captured["include_bot"] = include_bot
        captured["open_browser"] = open_browser
        captured["called"] = True

    monkeypatch.setattr(cli_module, "run_all_services", fake_run_all_services)
    monkeypatch.setattr("sys.argv", ["nutrition-agent", *argv])
    cli_module.main()
    return captured


def test_cli_web_flag_runs_dashboard_without_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """--web starts only the dashboard and returns before touching the database."""
    captured = _capture_service_call(monkeypatch, ["--web"])
    assert captured["called"] is True
    assert captured["include_bot"] is False
    assert captured["open_browser"] is False


def test_cli_web_flag_with_open_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """--web --open-browser forwards the browser request."""
    captured = _capture_service_call(monkeypatch, ["--web", "--open-browser"])
    assert captured["include_bot"] is False
    assert captured["open_browser"] is True


def test_cli_run_all_flag_includes_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """--run-all starts both services and can open the dashboard."""
    captured = _capture_service_call(monkeypatch, ["--run-all", "--open-browser"])
    assert captured["include_bot"] is True
    assert captured["open_browser"] is True


def test_cli_czech_aliases_for_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare 'web' and 'run' words work as service commands."""
    assert _capture_service_call(monkeypatch, ["web"])["include_bot"] is False
    assert _capture_service_call(monkeypatch, ["run"])["include_bot"] is True


def test_run_all_services_skips_bot_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing Discord token must not prevent the dashboard from starting."""
    import asyncio
    import sys

    import nutrition_agent.cli as cli_module

    settings_override = type(cli_module.settings)(
        database_path=cli_module.settings.database_path,
        foods_data_path=cli_module.settings.foods_data_path,
        log_level="WARNING",
        gemini_api_key=None,
        discord_bot_token=None,
        discord_guild_id=None,
        web_host="127.0.0.1",
        web_port=8000,
        target_calories=None,
        target_protein_g=None,
    )
    monkeypatch.setattr(cli_module, "settings", settings_override)

    started: list[str] = []

    class FakeServer:
        def __init__(self, config: object) -> None:
            self.started = True

        async def serve(self) -> None:
            started.append("web")
            await asyncio.sleep(0)

    class FakeUvicorn:
        Server = FakeServer

        class Config:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)

    cli_module.run_all_services(include_bot=True, open_browser=False)

    assert started == ["web"]


def test_start_bat_exists_and_calls_run_all() -> None:
    """The double-click launcher must exist and start both services."""
    from pathlib import Path

    bat_path = Path(__file__).resolve().parent.parent / "start.bat"
    assert bat_path.exists()

    content = bat_path.read_text(encoding="ascii")
    assert "--run-all" in content
    assert "--open-browser" in content
    assert ".venv\\Scripts\\python.exe" in content
    assert "pause" in content


def test_start_bat_stops_a_leftover_instance() -> None:
    """A stale server holding the port must be stopped before starting a new one.

    Otherwise the new process fails to bind with WinError 10048, the old one keeps
    answering requests, and the browser silently talks to outdated code. That is how
    the dashboard ended up with missing carb and fat cards plus 'Not Found' errors.
    """
    from pathlib import Path

    content = (Path(__file__).resolve().parent.parent / "start.bat").read_text(encoding="ascii")

    assert "netstat -ano" in content
    assert "taskkill /F /T /PID" in content
    assert "WEB_PORT" in content
    # The port is read from .env rather than hardcoded
    assert 'if /i "%%a"=="WEB_PORT"' in content
    # Killing returns before the socket is released, so the script must poll
    assert ":wait_port" in content
    assert "GEQ 15" in content
    assert "se nepodarilo uvolnit" in content
    # TIME_WAIT lines carry PID 0 and must not be killed
    assert '"0"' in content
