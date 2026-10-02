"""Tests for deleting a day of records across repository, CLI and Discord."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import (
    CalculatedFoodLog,
    CalculatedWeightLog,
    CalculatedWorkoutLog,
    parse_date_argument,
)
from nutrition_agent.repository import (
    count_day_logs,
    delete_day_logs,
    get_daily_food_logs,
    get_recent_weight_logs,
    get_recent_workout_logs,
    save_food_logs,
    save_weight_log,
    save_workout_log,
)
from tests.scratch import TEST_SCRATCH_ROOT


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


# Sandbox restricts the OS temp directory, so isolated test databases live in the workspace.
TEST_DB_DIR = TEST_SCRATCH_ROOT


@pytest.fixture
def temp_settings_db():
    """Settings pointing at an isolated database instead of the real one."""
    from nutrition_agent.config import settings as real_settings

    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    return type(real_settings)(
        database_path=TEST_DB_DIR / "delete_day_cli.db",
        foods_data_path=real_settings.foods_data_path,
        log_level="WARNING",
        gemini_api_key=None,
        discord_bot_token=None,
        discord_guild_id=None,
        web_host="127.0.0.1",
        web_port=8000,
        target_calories=None,
        target_protein_g=None,
    )


@pytest.fixture
def two_day_db() -> sqlite3.Connection:
    """Database with one food, weight and workout record on each of two days."""
    return _seeded_db()


def _seeded_db() -> sqlite3.Connection:
    """Build a database with one food, weight and workout record on two days."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)

    for day in ("2026-09-15", "2026-09-16"):
        save_food_logs(
            conn,
            [
                CalculatedFoodLog(
                    logged_at=f"{day}T12:00:00Z",
                    raw_input="200g kureci",
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
            CalculatedWeightLog(logged_at=f"{day}T07:00:00Z", raw_input="83 kg", weight_kg=83.0),
        )
        save_workout_log(
            conn,
            CalculatedWorkoutLog(
                logged_at=f"{day}T18:00:00Z",
                raw_input="trenink",
                workout_type="Silový trénink",
                duration_minutes=60,
            ),
        )

    return conn


def test_parse_date_argument_defaults_to_today():
    """Missing or blank input resolves to today."""
    assert parse_date_argument(None) == _today()
    assert parse_date_argument("") == _today()
    assert parse_date_argument("   ") == _today()


def test_parse_date_argument_keywords():
    """'dnes' and 'vcera' resolve to today and yesterday."""
    assert parse_date_argument("dnes") == _today()
    assert parse_date_argument("Dnes") == _today()
    assert parse_date_argument("today") == _today()
    assert parse_date_argument("vcera") == _yesterday()
    assert parse_date_argument("včera") == _yesterday()
    assert parse_date_argument("yesterday") == _yesterday()


def test_parse_date_argument_explicit_date():
    """Explicit ISO dates pass through, surrounding whitespace is trimmed."""
    assert parse_date_argument("2026-09-15") == "2026-09-15"
    assert parse_date_argument("  2026-01-02  ") == "2026-01-02"


@pytest.mark.parametrize("bad_value", ["16.9.2026", "2026/09/15", "vcer", "2026-13-01", "abc"])
def test_parse_date_argument_fails_fast(bad_value: str):
    """Unsupported formats raise a ValueError with a helpful message."""
    with pytest.raises(ValueError, match="Neplatné datum"):
        parse_date_argument(bad_value)


def test_count_day_logs_per_day(two_day_db: sqlite3.Connection):
    """Counts are reported per record type for the requested day only."""
    counts = count_day_logs(two_day_db, "2026-09-15")
    assert counts == {"food": 1, "weight": 1, "workout": 1, "day_type": 0}

    assert count_day_logs(two_day_db, "2026-01-01") == {
        "food": 0,
        "weight": 0,
        "workout": 0,
        "day_type": 0,
    }


def test_delete_day_logs_removes_only_that_day(two_day_db: sqlite3.Connection):
    """Deleting one day leaves the other day untouched."""
    deleted = delete_day_logs(two_day_db, "2026-09-15")
    assert deleted == {"food": 1, "weight": 1, "workout": 1, "day_type": 0}

    assert count_day_logs(two_day_db, "2026-09-15") == {
        "food": 0,
        "weight": 0,
        "workout": 0,
        "day_type": 0,
    }

    remaining = count_day_logs(two_day_db, "2026-09-16")
    assert remaining == {"food": 1, "weight": 1, "workout": 1, "day_type": 0}
    assert len(get_daily_food_logs(two_day_db, "2026-09-16")) == 1
    assert len(get_recent_weight_logs(two_day_db)) == 1
    assert len(get_recent_workout_logs(two_day_db)) == 1


def test_delete_day_logs_on_empty_day_reports_zeros(two_day_db: sqlite3.Connection):
    """Deleting a day without records is a no-op that reports zero counts."""
    assert delete_day_logs(two_day_db, "2026-01-01") == {
        "food": 0,
        "weight": 0,
        "workout": 0,
        "day_type": 0,
    }
    # Nothing else was touched
    assert count_day_logs(two_day_db, "2026-09-15") == {
        "food": 1,
        "weight": 1,
        "workout": 1,
        "day_type": 0,
    }


def test_delete_day_is_idempotent(two_day_db: sqlite3.Connection):
    """Running the same deletion twice is safe."""
    delete_day_logs(two_day_db, "2026-09-15")
    assert delete_day_logs(two_day_db, "2026-09-15") == {
        "food": 0,
        "weight": 0,
        "workout": 0,
        "day_type": 0,
    }


def test_delete_day_does_not_touch_user_settings(two_day_db: sqlite3.Connection):
    """User profile settings live outside day-scoped tables and must survive."""
    from nutrition_agent.repository import get_user_setting, set_user_setting

    set_user_setting(two_day_db, "goal", "cut")
    delete_day_logs(two_day_db, "2026-09-15")
    assert get_user_setting(two_day_db, "goal") == "cut"


def test_delete_day_also_clears_the_chosen_day_type(two_day_db: sqlite3.Connection):
    """Wiping a day takes its day type with it, so no phantom training day is left."""
    from nutrition_agent.repository import get_day_type, set_day_type
    from nutrition_agent.targets import DayType

    set_day_type(two_day_db, "2026-09-15", DayType.TRAINING)

    deleted = delete_day_logs(two_day_db, "2026-09-15")
    assert deleted["day_type"] == 1
    assert get_day_type(two_day_db, "2026-09-15") == DayType.REST


def test_cli_delete_day_command_with_explicit_date(two_day_db: sqlite3.Connection):
    """'vymaz <datum>' deletes that day and reports per-type counts."""
    from nutrition_agent.cli import delete_day, handle_user_message

    result = handle_user_message("vymaz 2026-09-15", [], two_day_db)
    assert "2026-09-15" in result
    assert "- Jídla: 1" in result
    assert "- Vážení: 1" in result
    assert "- Tréninky: 1" in result
    assert "Celkem smazáno 3 záznamů" in result

    assert count_day_logs(two_day_db, "2026-09-16") == {
        "food": 1,
        "weight": 1,
        "workout": 1,
        "day_type": 0,
    }

    # The standalone helper produces the same report
    assert delete_day(two_day_db, "2026-09-16").startswith("Vymazány záznamy pro den 2026-09-16")


def test_cli_delete_day_command_defaults_to_today():
    """'vymaz' without an argument deletes today."""
    conn = get_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    save_food_logs(
        conn,
        [
            CalculatedFoodLog(
                logged_at=f"{_today()}T12:00:00Z",
                raw_input="x",
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

    from nutrition_agent.cli import handle_user_message

    result = handle_user_message("vymaz", [], conn)
    assert _today() in result
    assert "Celkem smazáno 1 záznamů" in result
    assert count_day_logs(conn, _today())["food"] == 0


def test_cli_delete_day_command_accepts_czech_variants():
    """Diacritic and synonym variants of the command are recognised."""
    from nutrition_agent.cli import handle_user_message

    for command in ("vymaz", "vymaž", "smaz", "smaž"):
        conn = _seeded_db()
        result = handle_user_message(f"{command} 2026-09-15", [], conn)
        assert "Vymazány záznamy pro den 2026-09-15" in result, command
        assert count_day_logs(conn, "2026-09-15") == {
            "food": 0,
            "weight": 0,
            "workout": 0,
            "day_type": 0,
        }


def test_cli_delete_day_command_empty_day(two_day_db: sqlite3.Connection):
    """Deleting a day without records reports that nothing was found."""
    from nutrition_agent.cli import handle_user_message

    result = handle_user_message("vymaz 2026-01-01", [], two_day_db)
    assert result == "Pro den 2026-01-01 nebyly nalezeny žádné záznamy."


def test_cli_delete_day_command_invalid_date_fails_fast(two_day_db: sqlite3.Connection):
    """An unsupported date format returns an error and deletes nothing."""
    from nutrition_agent.cli import handle_user_message

    result = handle_user_message("vymaz 15.9.2026", [], two_day_db)
    assert "Neplatné datum" in result
    assert count_day_logs(two_day_db, "2026-09-15") == {
        "food": 1,
        "weight": 1,
        "workout": 1,
        "day_type": 0,
    }


def test_cli_delete_day_flag_routing(monkeypatch: pytest.MonkeyPatch, temp_settings_db):
    """--delete-day forwards the date and defaults to today without a value."""
    import nutrition_agent.cli as cli_module

    calls: list[str] = []

    def fake_delete_day(conn: sqlite3.Connection, day: str) -> str:
        calls.append(day)
        return f"smazano {day}"

    monkeypatch.setattr(cli_module, "delete_day", fake_delete_day)
    monkeypatch.setattr(cli_module, "settings", temp_settings_db)

    for argv, expected in (
        (["--delete-day", "2026-09-15"], "2026-09-15"),
        (["--delete-day", "vcera"], _yesterday()),
        (["--delete-day"], _today()),
    ):
        calls.clear()
        monkeypatch.setattr("sys.argv", ["nutrition-agent", *argv])
        cli_module.main()
        assert calls == [expected]


def test_discord_delete_view_has_confirm_and_cancel():
    """The Discord confirmation view offers one danger and one secondary button."""
    import discord

    from nutrition_agent.bot import ConfirmDeleteDayView

    view = ConfirmDeleteDayView("2026-09-15")
    assert view.day == "2026-09-15"
    assert len(view.children) == 2

    buttons = {child.label: child for child in view.children}
    assert set(buttons) == {"Vymazat", "Zrušit"}
    assert buttons["Vymazat"].style == discord.ButtonStyle.danger
    assert buttons["Zrušit"].style == discord.ButtonStyle.secondary
    assert all(not button.disabled for button in buttons.values())


def test_discord_bot_registers_vymaz_command():
    """The /vymaz slash command is registered alongside the others."""
    from nutrition_agent.bot import create_bot

    _, tree = create_bot()
    command_names = {cmd.name for cmd in tree.get_commands()}
    assert "vymaz" in command_names
