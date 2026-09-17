"""Data access repository for foods and logs."""

import json
import sqlite3
from pathlib import Path

from nutrition_agent.models import (
    CalculatedFoodLog,
    CalculatedWeightLog,
    FoodDefinition,
    utc_now_iso,
)
from nutrition_agent.targets import DayType, DietGoal


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


def get_daily_food_logs(
    conn: sqlite3.Connection,
    date_prefix: str,
) -> list[CalculatedFoodLog]:
    """Fetch food logs for a given day (date_prefix in ISO 8601, e.g. 'YYYY-MM-DD')."""
    sql = """
    SELECT id, logged_at, raw_input, food_name_matched, amount_g,
           state, is_estimated, calories, protein_raw, protein_usable, carbs, fats
    FROM food_logs
    WHERE logged_at LIKE ? || '%'
    ORDER BY logged_at ASC
    """
    cursor = conn.cursor()
    cursor.execute(sql, (date_prefix,))
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


def get_latest_weight_log(
    conn: sqlite3.Connection,
    date_prefix: str | None = None,
) -> CalculatedWeightLog | None:
    """Fetch the latest weight log, optionally filtered by date_prefix."""
    cursor = conn.cursor()
    if date_prefix:
        sql = """
        SELECT id, logged_at, raw_input, weight_kg
        FROM weight_logs
        WHERE logged_at LIKE ? || '%'
        ORDER BY logged_at DESC
        LIMIT 1
        """
        cursor.execute(sql, (date_prefix,))
    else:
        sql = """
        SELECT id, logged_at, raw_input, weight_kg
        FROM weight_logs
        ORDER BY logged_at DESC
        LIMIT 1
        """
        cursor.execute(sql)

    row = cursor.fetchone()
    if row is None:
        return None

    return CalculatedWeightLog(
        id=row["id"],
        logged_at=row["logged_at"],
        raw_input=row["raw_input"],
        weight_kg=row["weight_kg"],
    )


def get_user_setting(
    conn: sqlite3.Connection,
    key: str,
    default: str | None = None,
) -> str | None:
    """Retrieve user setting value by key."""
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM user_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row["value"] if row else default


def set_user_setting(
    conn: sqlite3.Connection,
    key: str,
    value: str,
) -> None:
    """Persist or update user setting."""
    sql = """
    INSERT INTO user_settings (key, value, updated_at)
    VALUES (?, ?, ?)
    ON CONFLICT(key) DO UPDATE SET
        value = excluded.value,
        updated_at = excluded.updated_at
    """
    cursor = conn.cursor()
    cursor.execute(sql, (key, value, utc_now_iso()))
    conn.commit()


def get_user_profile(conn: sqlite3.Connection) -> tuple[DietGoal, DayType, float]:
    """Return active (DietGoal, DayType, current_weight_kg)."""
    raw_goal = get_user_setting(conn, "goal", DietGoal.LEAN_BULK.value) or DietGoal.LEAN_BULK.value
    raw_day_type = (
        get_user_setting(conn, "day_type", DayType.TRAINING.value) or DayType.TRAINING.value
    )

    try:
        goal = DietGoal(raw_goal)
    except ValueError:
        goal = DietGoal.LEAN_BULK

    try:
        day_type = DayType(raw_day_type)
    except ValueError:
        day_type = DayType.TRAINING

    latest_weight = get_latest_weight_log(conn)
    if latest_weight is not None:
        weight_kg = latest_weight.weight_kg
    else:
        ref_weight_str = get_user_setting(conn, "reference_weight", "83.0") or "83.0"
        try:
            weight_kg = float(ref_weight_str)
        except (ValueError, TypeError):
            weight_kg = 83.0

    return goal, day_type, weight_kg


def get_food_logs_in_range(
    conn: sqlite3.Connection,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> list[CalculatedFoodLog]:
    """Fetch food logs within optional ISO timestamp boundaries."""
    conditions: list[str] = []
    params: list[str] = []

    if start_iso:
        conditions.append("logged_at >= ?")
        params.append(start_iso)
    if end_iso:
        conditions.append("logged_at <= ?")
        params.append(end_iso)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
    SELECT id, logged_at, raw_input, food_name_matched, amount_g,
           state, is_estimated, calories, protein_raw, protein_usable, carbs, fats
    FROM food_logs
    {where_clause}
    ORDER BY logged_at ASC
    """
    cursor = conn.cursor()
    cursor.execute(sql, tuple(params))
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


def get_weight_logs_in_range(
    conn: sqlite3.Connection,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> list[CalculatedWeightLog]:
    """Fetch weight logs within optional ISO timestamp boundaries."""
    conditions: list[str] = []
    params: list[str] = []

    if start_iso:
        conditions.append("logged_at >= ?")
        params.append(start_iso)
    if end_iso:
        conditions.append("logged_at <= ?")
        params.append(end_iso)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
    SELECT id, logged_at, raw_input, weight_kg
    FROM weight_logs
    {where_clause}
    ORDER BY logged_at ASC
    """
    cursor = conn.cursor()
    cursor.execute(sql, tuple(params))
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
