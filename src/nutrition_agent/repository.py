"""Data access repository for foods and logs."""

import json
import sqlite3
from pathlib import Path

from nutrition_agent.models import (
    CalculatedFoodLog,
    CalculatedWeightLog,
    FoodDefinition,
)


def load_foods_from_json(file_path: str | Path = "data/foods.json") -> list[FoodDefinition]:
    """Load food definitions from local JSON file."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Database file not found: {file_path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    return [FoodDefinition.model_validate(item) for item in data]


def save_food_logs(
    conn: sqlite3.Connection,
    logs: list[CalculatedFoodLog],
) -> list[CalculatedFoodLog]:
    """Persist calculated food logs in a transaction."""
    if not logs:
        return []

    sql = """
    INSERT INTO food_logs (
        logged_at, raw_input, food_name_matched, amount_g,
        state, is_estimated, calories, protein_raw, protein_usable, carbs, fats
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    saved_logs: list[CalculatedFoodLog] = []
    cursor = conn.cursor()
    for log in logs:
        cursor.execute(
            sql,
            (
                log.logged_at,
                log.raw_input,
                log.food_name_matched,
                log.amount_g,
                log.state,
                1 if log.is_estimated else 0,
                log.calories,
                log.protein_raw,
                log.protein_usable,
                log.carbs,
                log.fats,
            ),
        )
        saved_logs.append(log.model_copy(update={"id": cursor.lastrowid}))
    conn.commit()
    return saved_logs


def save_weight_log(
    conn: sqlite3.Connection,
    log: CalculatedWeightLog,
) -> CalculatedWeightLog:
    """Persist calculated body weight log."""
    sql = """
    INSERT INTO weight_logs (logged_at, raw_input, weight_kg)
    VALUES (?, ?, ?)
    """
    cursor = conn.cursor()
    cursor.execute(sql, (log.logged_at, log.raw_input, log.weight_kg))
    conn.commit()
    return log.model_copy(update={"id": cursor.lastrowid})


def get_recent_food_logs(
    conn: sqlite3.Connection,
    limit: int = 50,
) -> list[CalculatedFoodLog]:
    """Fetch recent food logs ordered descending by timestamp."""
    sql = """
    SELECT id, logged_at, raw_input, food_name_matched, amount_g,
           state, is_estimated, calories, protein_raw, protein_usable, carbs, fats
    FROM food_logs
    ORDER BY logged_at DESC
    LIMIT ?
    """
    cursor = conn.cursor()
    cursor.execute(sql, (limit,))
    rows = cursor.fetchall()
    return [
        CalculatedFoodLog(
            id=row["id"],
            logged_at=row["logged_at"],
            raw_input=row["raw_input"],
            food_name_matched=row["food_name_matched"],
            amount_g=row["amount_g"],
            state=row["state"],
            is_estimated=bool(row["is_estimated"]),
            calories=row["calories"],
            protein_raw=row["protein_raw"],
            protein_usable=row["protein_usable"],
            carbs=row["carbs"],
            fats=row["fats"],
        )
        for row in rows
    ]


def get_recent_weight_logs(
    conn: sqlite3.Connection,
    limit: int = 50,
) -> list[CalculatedWeightLog]:
    """Fetch recent weight logs ordered descending by timestamp."""
    sql = """
    SELECT id, logged_at, raw_input, weight_kg
    FROM weight_logs
    ORDER BY logged_at DESC
    LIMIT ?
    """
    cursor = conn.cursor()
    cursor.execute(sql, (limit,))
    rows = cursor.fetchall()
    return [
        CalculatedWeightLog(
            id=row["id"],
            logged_at=row["logged_at"],
            raw_input=row["raw_input"],
            weight_kg=row["weight_kg"],
        )
        for row in rows
    ]
