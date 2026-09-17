"""Integration tests for CLI message handling and calculation pipeline."""

import sqlite3

import pytest

from nutrition_agent.cli import handle_user_message
from nutrition_agent.db import SCHEMA_SQL, get_connection
from nutrition_agent.models import ExtractedFoodItem, FoodDefinition, UserLog


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


def test_handle_unknown_food_fails_cleanly(
    test_setup: tuple[sqlite3.Connection, list[FoodDefinition]],
) -> None:
    """Unknown food returns error prompt without saving to DB."""
    conn, foods = test_setup

    def mock_parser(msg: str) -> UserLog:
        return UserLog(
            log_type="FOOD",
            items=[
                ExtractedFoodItem(
                    name="neznámá věc xyz",
                    amount=100.0,
                    unit="g",
                    state="raw",
                    is_estimated=False,
                )
            ],
        )

    result = handle_user_message("100g neznámá věc xyz", foods, conn, parse_fn=mock_parser)
    assert "Chyba: Surovina 'neznámá věc xyz' nebyla nalezena v databázi" in result

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
