"""Data access repository for foods and logs."""

import json
import sqlite3
from pathlib import Path

from nutrition_agent.models import (
    CalculatedFoodLog,
    CalculatedWeightLog,
    CalculatedWorkoutLog,
    FoodDefinition,
    food_name_key,
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


FOOD_COLUMNS = "name, calories, protein, diaas_coefficient, carbs, fats, source"


def _row_to_food(row: sqlite3.Row) -> FoodDefinition:
    """Convert a foods row into a FoodDefinition model."""
    return FoodDefinition(
        name=row["name"],
        calories=row["calories"],
        protein=row["protein"],
        diaas_coefficient=row["diaas_coefficient"],
        carbs=row["carbs"],
        fats=row["fats"],
        source=row["source"],
    )


def ensure_food_catalog(conn: sqlite3.Connection, json_path: str | Path) -> int:
    """Seed curated foods from the JSON catalog into the foods table.

    Idempotent: existing rows (including user-corrected or AI-added ones) are
    never overwritten, so only missing curated foods are inserted.
    """
    foods = load_foods_from_json(json_path)
    now = utc_now_iso()
    cursor = conn.cursor()
    inserted = 0
    for food in foods:
        cursor.execute(
            """
            INSERT INTO foods
                (name, name_key, calories, protein, diaas_coefficient, carbs, fats,
                 source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'curated', ?, ?)
            ON CONFLICT(name_key) DO NOTHING
            """,
            (
                food.name,
                food_name_key(food.name),
                food.calories,
                food.protein,
                food.diaas_coefficient,
                food.carbs,
                food.fats,
                now,
                now,
            ),
        )
        inserted += cursor.rowcount
    conn.commit()
    return inserted


def load_foods(conn: sqlite3.Connection, source: str | None = None) -> list[FoodDefinition]:
    """Load food definitions from the foods table.

    Curated catalog entries come first so that fuzzy matching prefers them over
    AI-estimated entries when scores tie.
    """
    sql = f"SELECT {FOOD_COLUMNS} FROM foods"
    params: tuple[str, ...] = ()
    if source:
        sql += " WHERE source = ?"
        params = (source,)
    sql += " ORDER BY (source = 'curated') DESC, name ASC"

    return [_row_to_food(row) for row in conn.execute(sql, params).fetchall()]


def get_food(conn: sqlite3.Connection, name: str) -> FoodDefinition | None:
    """Fetch a single food by name, ignoring case and redundant whitespace."""
    row = conn.execute(
        f"SELECT {FOOD_COLUMNS} FROM foods WHERE name_key = ?",
        (food_name_key(name),),
    ).fetchone()
    return _row_to_food(row) if row is not None else None


def save_learned_food(conn: sqlite3.Connection, food: FoodDefinition) -> bool:
    """Store an AI-estimated food so later logs reuse the same numbers.

    Returns True when a new row was inserted, False when the food already existed.
    """
    if food.source != "ai":
        raise ValueError("save_learned_food ukládá pouze odhady s source='ai'.")

    now = utc_now_iso()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO foods
            (name, name_key, calories, protein, diaas_coefficient, carbs, fats,
             source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?)
        ON CONFLICT(name_key) DO NOTHING
        """,
        (
            food.name,
            food_name_key(food.name),
            food.calories,
            food.protein,
            food.diaas_coefficient,
            food.carbs,
            food.fats,
            now,
            now,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def update_food_nutrition(
    conn: sqlite3.Connection,
    name: str,
    calories: float,
    protein: float,
    carbs: float,
    fats: float,
) -> FoodDefinition:
    """Overwrite the nutrition values of an existing food (manual correction)."""
    existing = get_food(conn, name)
    if existing is None:
        raise LookupError(f"Potravina '{name}' není v katalogu.")

    conn.execute(
        """
        UPDATE foods
        SET calories = ?, protein = ?, carbs = ?, fats = ?, updated_at = ?
        WHERE name_key = ?
        """,
        (calories, protein, carbs, fats, utc_now_iso(), food_name_key(existing.name)),
    )
    conn.commit()

    updated = get_food(conn, existing.name)
    if updated is None:
        raise LookupError(f"Potravina '{name}' po aktualizaci zmizela.")
    return updated


def list_foods_by_source(conn: sqlite3.Connection, source: str) -> list[FoodDefinition]:
    """List foods for one source layer ('curated' or 'ai')."""
    return load_foods(conn, source=source)


def delete_food(conn: sqlite3.Connection, name: str) -> FoodDefinition:
    """Remove a food from the catalog. Raises LookupError when it does not exist."""
    existing = get_food(conn, name)
    if existing is None:
        raise LookupError(f"Potravina '{name}' není v katalogu.")

    conn.execute("DELETE FROM foods WHERE name_key = ?", (food_name_key(existing.name),))
    conn.execute("DELETE FROM unit_conversions WHERE food_key = ?", (food_name_key(existing.name),))
    conn.commit()
    return existing


def get_unit_conversion(
    conn: sqlite3.Connection,
    food_name: str,
    unit: str,
) -> tuple[float, str] | None:
    """Return (grams_per_unit, source) for a learned conversion, or None."""
    row = conn.execute(
        """
        SELECT grams_per_unit, source
        FROM unit_conversions
        WHERE food_key = ? AND unit = ?
        """,
        (food_name_key(food_name), unit.strip().lower()),
    ).fetchone()
    if row is None:
        return None
    return float(row["grams_per_unit"]), str(row["source"])


def save_unit_conversion(
    conn: sqlite3.Connection,
    food_name: str,
    unit: str,
    grams_per_unit: float,
    source: str = "ai",
) -> None:
    """Store or overwrite the grams-per-unit conversion for a food."""
    if source not in ("user", "ai"):
        raise ValueError("Zdroj převodu musí být 'user' nebo 'ai'.")

    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO unit_conversions
            (food_key, unit, grams_per_unit, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(food_key, unit) DO UPDATE SET
            grams_per_unit = excluded.grams_per_unit,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (
            food_name_key(food_name),
            unit.strip().lower(),
            grams_per_unit,
            source,
            now,
            now,
        ),
    )
    conn.commit()


def delete_unit_conversion(
    conn: sqlite3.Connection,
    food_name: str,
    unit: str,
) -> bool:
    """Remove a learned conversion. Returns True when a row was deleted."""
    cursor = conn.execute(
        "DELETE FROM unit_conversions WHERE food_key = ? AND unit = ?",
        (food_name_key(food_name), unit.strip().lower()),
    )
    conn.commit()
    return cursor.rowcount > 0


def list_unit_conversions(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """List every learned conversion, newest first."""
    rows = conn.execute(
        """
        SELECT food_key, unit, grams_per_unit, source, updated_at
        FROM unit_conversions
        ORDER BY updated_at DESC, food_key ASC
        """
    ).fetchall()
    return [
        {
            "food_key": row["food_key"],
            "unit": row["unit"],
            "grams_per_unit": float(row["grams_per_unit"]),
            "source": row["source"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


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
        state, is_estimated, calories, protein_raw, protein_usable, carbs, fats,
        nutrition_source, conversion_estimated
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                log.nutrition_source,
                1 if log.conversion_estimated else 0,
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


def _row_to_food_log(row: sqlite3.Row) -> CalculatedFoodLog:
    """Convert a food_logs row into a CalculatedFoodLog model."""
    return CalculatedFoodLog(
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
        nutrition_source=row["nutrition_source"],
        conversion_estimated=bool(row["conversion_estimated"]),
    )


def get_recent_food_logs(
    conn: sqlite3.Connection,
    limit: int = 50,
) -> list[CalculatedFoodLog]:
    """Fetch recent food logs ordered descending by timestamp."""
    sql = """
    SELECT id, logged_at, raw_input, food_name_matched, amount_g,
           state, is_estimated, calories, protein_raw, protein_usable, carbs, fats,
           nutrition_source, conversion_estimated
    FROM food_logs
    ORDER BY logged_at DESC
    LIMIT ?
    """
    cursor = conn.cursor()
    cursor.execute(sql, (limit,))
    return [_row_to_food_log(row) for row in cursor.fetchall()]


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
           state, is_estimated, calories, protein_raw, protein_usable, carbs, fats,
           nutrition_source, conversion_estimated
    FROM food_logs
    WHERE logged_at LIKE ? || '%'
    ORDER BY logged_at ASC
    """
    cursor = conn.cursor()
    cursor.execute(sql, (date_prefix,))
    return [_row_to_food_log(row) for row in cursor.fetchall()]


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


def get_daily_weight_log(
    conn: sqlite3.Connection,
    date_prefix: str,
) -> CalculatedWeightLog | None:
    """Fetch the latest weight log for a given day (e.g. 'YYYY-MM-DD')."""
    return get_latest_weight_log(conn, date_prefix)


def get_daily_weight_comparison(
    conn: sqlite3.Connection,
    date_prefix: str,
) -> tuple[CalculatedWeightLog | None, float | None]:
    """Return today's weight log and delta against the previous logged day."""
    from datetime import datetime, timedelta

    current = get_daily_weight_log(conn, date_prefix)
    if current is None:
        return None, None

    ref_date = datetime.strptime(date_prefix[:10], "%Y-%m-%d")
    previous: CalculatedWeightLog | None = None
    for days_back in range(1, 8):
        prev_prefix = (ref_date - timedelta(days=days_back)).strftime("%Y-%m-%d")
        previous = get_daily_weight_log(conn, prev_prefix)
        if previous is not None:
            break

    if previous is None:
        return current, None

    return current, round(current.weight_kg - previous.weight_kg, 1)


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
           state, is_estimated, calories, protein_raw, protein_usable, carbs, fats,
           nutrition_source, conversion_estimated
    FROM food_logs
    {where_clause}
    ORDER BY logged_at ASC
    """
    cursor = conn.cursor()
    cursor.execute(sql, tuple(params))
    return [_row_to_food_log(row) for row in cursor.fetchall()]


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


def save_workout_log(
    conn: sqlite3.Connection,
    log: CalculatedWorkoutLog,
) -> CalculatedWorkoutLog:
    """Persist workout log."""
    sql = """
    INSERT INTO workout_logs (logged_at, raw_input, workout_type, duration_minutes, notes)
    VALUES (?, ?, ?, ?, ?)
    """
    cursor = conn.cursor()
    cursor.execute(
        sql,
        (
            log.logged_at,
            log.raw_input,
            log.workout_type,
            log.duration_minutes,
            log.notes,
        ),
    )
    conn.commit()
    return log.model_copy(update={"id": cursor.lastrowid})


def get_recent_workout_logs(
    conn: sqlite3.Connection,
    limit: int = 50,
) -> list[CalculatedWorkoutLog]:
    """Fetch recent workout logs ordered descending by timestamp."""
    sql = """
    SELECT id, logged_at, raw_input, workout_type, duration_minutes, notes
    FROM workout_logs
    ORDER BY logged_at DESC
    LIMIT ?
    """
    cursor = conn.cursor()
    cursor.execute(sql, (limit,))
    rows = cursor.fetchall()
    return [
        CalculatedWorkoutLog(
            id=row["id"],
            logged_at=row["logged_at"],
            raw_input=row["raw_input"],
            workout_type=row["workout_type"],
            duration_minutes=row["duration_minutes"],
            notes=row["notes"],
        )
        for row in rows
    ]


def get_workout_logs_in_range(
    conn: sqlite3.Connection,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> list[CalculatedWorkoutLog]:
    """Fetch workout logs within optional ISO timestamp boundaries."""
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
    SELECT id, logged_at, raw_input, workout_type, duration_minutes, notes
    FROM workout_logs
    {where_clause}
    ORDER BY logged_at ASC
    """
    cursor = conn.cursor()
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()
    return [
        CalculatedWorkoutLog(
            id=row["id"],
            logged_at=row["logged_at"],
            raw_input=row["raw_input"],
            workout_type=row["workout_type"],
            duration_minutes=row["duration_minutes"],
            notes=row["notes"],
        )
        for row in rows
    ]


def get_daily_workout_log(
    conn: sqlite3.Connection,
    date_prefix: str,
) -> CalculatedWorkoutLog | None:
    """Fetch latest workout log for a given day (e.g. 'YYYY-MM-DD')."""
    sql = """
    SELECT id, logged_at, raw_input, workout_type, duration_minutes, notes
    FROM workout_logs
    WHERE logged_at LIKE ? || '%'
    ORDER BY logged_at DESC
    LIMIT 1
    """
    cursor = conn.cursor()
    cursor.execute(sql, (date_prefix,))
    row = cursor.fetchone()
    if row is None:
        return None
    return CalculatedWorkoutLog(
        id=row["id"],
        logged_at=row["logged_at"],
        raw_input=row["raw_input"],
        workout_type=row["workout_type"],
        duration_minutes=row["duration_minutes"],
        notes=row["notes"],
    )


def get_weight_delta(
    conn: sqlite3.Connection,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> dict[str, float | None]:
    """Calculate weight statistics and delta over given period."""
    logs = get_weight_logs_in_range(conn, start_iso, end_iso)
    if not logs:
        latest = get_latest_weight_log(conn)
        w = latest.weight_kg if latest else None
        return {
            "start_weight": w,
            "current_weight": w,
            "delta_kg": 0.0 if w is not None else None,
            "min_weight": w,
            "max_weight": w,
            "count": 0,
        }

    weights = [log.weight_kg for log in logs]
    start_w = weights[0]
    curr_w = weights[-1]
    return {
        "start_weight": round(start_w, 1),
        "current_weight": round(curr_w, 1),
        "delta_kg": round(curr_w - start_w, 1),
        "min_weight": round(min(weights), 1),
        "max_weight": round(max(weights), 1),
        "count": len(weights),
    }


def get_weight_trend(
    conn: sqlite3.Connection,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> list[dict[str, object]]:
    """Compute weight trend points with 7-day rolling moving average."""
    from datetime import datetime, timedelta

    logs = get_weight_logs_in_range(conn, start_iso, end_iso)
    if not logs:
        return []

    day_weights: dict[str, float] = {}
    for log in logs:
        d = log.logged_at[:10]
        day_weights[d] = log.weight_kg

    sorted_dates = sorted(day_weights.keys())
    results: list[dict[str, object]] = []

    for d_str in sorted_dates:
        curr_dt = datetime.strptime(d_str, "%Y-%m-%d")
        window_start = curr_dt - timedelta(days=6)

        window_weights = [
            w
            for d, w in day_weights.items()
            if window_start <= datetime.strptime(d, "%Y-%m-%d") <= curr_dt
        ]
        moving_avg = (
            sum(window_weights) / len(window_weights) if window_weights else day_weights[d_str]
        )

        results.append(
            {
                "date": d_str,
                "weight_kg": round(day_weights[d_str], 1),
                "moving_avg_7d": round(moving_avg, 2),
            }
        )

    return results


def get_dashboard_kpis(
    conn: sqlite3.Connection,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> dict[str, object]:
    """Aggregate high-level KPIs for dashboard header cards."""
    from collections import defaultdict

    weight_stats = get_weight_delta(conn, start_iso, end_iso)
    food_logs = get_food_logs_in_range(conn, start_iso, end_iso)
    workout_logs = get_workout_logs_in_range(conn, start_iso, end_iso)

    daily_cals: dict[str, float] = defaultdict(float)
    daily_prots: dict[str, float] = defaultdict(float)

    for f in food_logs:
        d = f.logged_at[:10]
        daily_cals[d] += f.calories
        daily_prots[d] += f.protein_usable

    days_logged = len(daily_cals)
    total_cals = sum(daily_cals.values())
    total_prots = sum(daily_prots.values())

    avg_cals = (total_cals / days_logged) if days_logged > 0 else 0.0
    avg_prots = (total_prots / days_logged) if days_logged > 0 else 0.0

    workout_days = {w.logged_at[:10] for w in workout_logs}

    return {
        "current_weight": weight_stats["current_weight"],
        "weight_delta": weight_stats["delta_kg"],
        "avg_calories": round(avg_cals, 0),
        "avg_protein_diaas": round(avg_prots, 1),
        "total_workout_days": len(workout_days),
        "total_food_days": days_logged,
    }


def get_heatmap_data(
    conn: sqlite3.Connection,
    start_iso: str | None = None,
    end_iso: str | None = None,
    target_calories: float = 3150.0,
    target_protein: float = 155.0,
) -> list[dict[str, object]]:
    """Compute day-by-day matrix for GitHub-style adherence calendar heatmap."""
    from collections import defaultdict
    from datetime import datetime, timedelta

    food_logs = get_food_logs_in_range(conn, start_iso, end_iso)
    workout_logs = get_workout_logs_in_range(conn, start_iso, end_iso)

    daily_cals: dict[str, float] = defaultdict(float)
    daily_prots: dict[str, float] = defaultdict(float)
    for f in food_logs:
        d = f.logged_at[:10]
        daily_cals[d] += f.calories
        daily_prots[d] += f.protein_usable

    daily_workouts: dict[str, str] = {}
    for w in workout_logs:
        d = w.logged_at[:10]
        daily_workouts[d] = w.workout_type

    if start_iso and end_iso:
        dt_start = datetime.strptime(start_iso[:10], "%Y-%m-%d")
        dt_end = datetime.strptime(end_iso[:10], "%Y-%m-%d")
    else:
        all_dates = list(daily_cals.keys()) + list(daily_workouts.keys())
        if all_dates:
            dt_start = datetime.strptime(min(all_dates), "%Y-%m-%d")
            dt_end = datetime.strptime(max(all_dates), "%Y-%m-%d")
        else:
            dt_end = datetime.now()
            dt_start = dt_end - timedelta(days=29)

    heatmap: list[dict[str, object]] = []
    curr = dt_start
    while curr <= dt_end:
        d_str = curr.strftime("%Y-%m-%d")
        cal = daily_cals.get(d_str, 0.0)
        prot = daily_prots.get(d_str, 0.0)
        has_workout = d_str in daily_workouts
        workout_type = daily_workouts.get(d_str)

        if cal == 0.0 and prot == 0.0:
            level = "empty"
        elif prot >= (target_protein * 0.90) and (
            target_calories * 0.85 <= cal <= target_calories * 1.15
        ):
            level = "green"
        elif prot >= (target_protein * 0.90):
            level = "blue"
        else:
            level = "amber"

        heatmap.append(
            {
                "date": d_str,
                "weekday": curr.weekday(),
                "calories": round(cal, 1),
                "protein_usable": round(prot, 1),
                "has_workout": has_workout,
                "workout_type": workout_type,
                "level": level,
            }
        )
        curr += timedelta(days=1)

    return heatmap


def get_unified_records(
    conn: sqlite3.Connection,
    record_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, object]], int]:
    """Fetch paginated, filtered unified activity history across all entity types."""
    records: list[dict[str, object]] = []

    if record_type in (None, "all", "food"):
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, logged_at, raw_input, food_name_matched, amount_g, state,
                   calories, protein_raw, protein_usable, carbs, fats, nutrition_source,
                   conversion_estimated
            FROM food_logs
            ORDER BY logged_at DESC
            """
        )
        for row in cursor.fetchall():
            ai_flag = " [AI odhad]" if row["nutrition_source"] == "ai" else ""
            food_desc = (
                f"{row['food_name_matched']} ({row['amount_g']:.0f} g, {row['state']}){ai_flag}"
            )
            records.append(
                {
                    "type": "food",
                    "id": row["id"],
                    "logged_at": row["logged_at"],
                    "title": food_desc,
                    "subtitle": (
                        f"{row['calories']:.1f} kcal | B: {row['protein_usable']:.1f} g DIAAS | "
                        f"S: {row['carbs']:.1f} g | T: {row['fats']:.1f} g"
                    ),
                    "raw_input": row["raw_input"],
                    "calories": row["calories"],
                    "protein_usable": row["protein_usable"],
                    "carbs": row["carbs"],
                    "fats": row["fats"],
                    "amount_g": row["amount_g"],
                    "state": row["state"],
                    "nutrition_source": row["nutrition_source"],
                    "conversion_estimated": bool(row["conversion_estimated"]),
                }
            )

    if record_type in (None, "all", "weight"):
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, logged_at, raw_input, weight_kg FROM weight_logs ORDER BY logged_at DESC"
        )
        for row in cursor.fetchall():
            records.append(
                {
                    "type": "weight",
                    "id": row["id"],
                    "logged_at": row["logged_at"],
                    "title": f"Tělesná hmotnost: {row['weight_kg']:.1f} kg",
                    "subtitle": f"Záznam: '{row['raw_input']}'",
                    "raw_input": row["raw_input"],
                    "weight_kg": row["weight_kg"],
                }
            )

    if record_type in (None, "all", "workout"):
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, logged_at, raw_input, workout_type, duration_minutes, notes
            FROM workout_logs
            ORDER BY logged_at DESC
            """
        )
        for row in cursor.fetchall():
            dur_str = f" ({row['duration_minutes']} min)" if row["duration_minutes"] else ""
            records.append(
                {
                    "type": "workout",
                    "id": row["id"],
                    "logged_at": row["logged_at"],
                    "title": f"{row['workout_type']}{dur_str}",
                    "subtitle": row["notes"] or f"Záznam: '{row['raw_input']}'",
                    "raw_input": row["raw_input"],
                    "workout_type": row["workout_type"],
                    "duration_minutes": row["duration_minutes"],
                    "notes": row["notes"],
                }
            )

    records.sort(key=lambda r: str(r["logged_at"]), reverse=True)
    total_count = len(records)
    paginated = records[offset : offset + limit]
    return paginated, total_count


# Table names are fixed constants, never user input.
DAY_LOG_TABLES: dict[str, str] = {
    "food": "food_logs",
    "weight": "weight_logs",
    "workout": "workout_logs",
}

DAY_LOG_LABELS: dict[str, str] = {
    "food": "Jídla",
    "weight": "Vážení",
    "workout": "Tréninky",
}


def count_day_logs(conn: sqlite3.Connection, date_prefix: str) -> dict[str, int]:
    """Count food, weight and workout records for a given day."""
    cursor = conn.cursor()
    counts: dict[str, int] = {}
    for key, table in DAY_LOG_TABLES.items():
        cursor.execute(
            f"SELECT COUNT(*) FROM {table} WHERE logged_at LIKE ? || '%'",
            (date_prefix,),
        )
        counts[key] = int(cursor.fetchone()[0])
    return counts


def delete_day_logs(conn: sqlite3.Connection, date_prefix: str) -> dict[str, int]:
    """Delete every food, weight and workout record for a given day.

    Returns the number of deleted rows per record type.
    """
    cursor = conn.cursor()
    deleted: dict[str, int] = {}
    for key, table in DAY_LOG_TABLES.items():
        cursor.execute(
            f"DELETE FROM {table} WHERE logged_at LIKE ? || '%'",
            (date_prefix,),
        )
        deleted[key] = int(cursor.rowcount)
    conn.commit()
    return deleted


def get_food_log(conn: sqlite3.Connection, log_id: int) -> CalculatedFoodLog | None:
    """Fetch a single food log by id."""
    row = conn.execute(
        """
        SELECT id, logged_at, raw_input, food_name_matched, amount_g,
               state, is_estimated, calories, protein_raw, protein_usable, carbs, fats,
               nutrition_source, conversion_estimated
        FROM food_logs WHERE id = ?
        """,
        (log_id,),
    ).fetchone()
    return _row_to_food_log(row) if row is not None else None


def get_weight_log(conn: sqlite3.Connection, log_id: int) -> CalculatedWeightLog | None:
    """Fetch a single weight log by id."""
    row = conn.execute(
        "SELECT id, logged_at, raw_input, weight_kg FROM weight_logs WHERE id = ?",
        (log_id,),
    ).fetchone()
    if row is None:
        return None
    return CalculatedWeightLog(
        id=row["id"],
        logged_at=row["logged_at"],
        raw_input=row["raw_input"],
        weight_kg=row["weight_kg"],
    )


def get_workout_log(conn: sqlite3.Connection, log_id: int) -> CalculatedWorkoutLog | None:
    """Fetch a single workout log by id."""
    row = conn.execute(
        """
        SELECT id, logged_at, raw_input, workout_type, duration_minutes, notes
        FROM workout_logs WHERE id = ?
        """,
        (log_id,),
    ).fetchone()
    if row is None:
        return None
    return CalculatedWorkoutLog(
        id=row["id"],
        logged_at=row["logged_at"],
        raw_input=row["raw_input"],
        workout_type=row["workout_type"],
        duration_minutes=row["duration_minutes"],
        notes=row["notes"],
    )


def update_food_log(conn: sqlite3.Connection, log: CalculatedFoodLog) -> CalculatedFoodLog:
    """Overwrite a stored food log, including its recomputed macros."""
    if log.id is None:
        raise ValueError("Pro aktualizaci musí mít záznam své id.")

    cursor = conn.execute(
        """
        UPDATE food_logs
        SET food_name_matched = ?, amount_g = ?, state = ?, is_estimated = ?,
            calories = ?, protein_raw = ?, protein_usable = ?, carbs = ?, fats = ?,
            nutrition_source = ?, conversion_estimated = ?
        WHERE id = ?
        """,
        (
            log.food_name_matched,
            log.amount_g,
            log.state,
            1 if log.is_estimated else 0,
            log.calories,
            log.protein_raw,
            log.protein_usable,
            log.carbs,
            log.fats,
            log.nutrition_source,
            1 if log.conversion_estimated else 0,
            log.id,
        ),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise LookupError(f"Záznam jídla {log.id} nebyl nalezen.")
    return log


def update_weight_log(conn: sqlite3.Connection, log: CalculatedWeightLog) -> CalculatedWeightLog:
    """Overwrite a stored weight log."""
    if log.id is None:
        raise ValueError("Pro aktualizaci musí mít záznam své id.")

    cursor = conn.execute(
        "UPDATE weight_logs SET weight_kg = ? WHERE id = ?",
        (log.weight_kg, log.id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise LookupError(f"Záznam váhy {log.id} nebyl nalezen.")
    return log


def update_workout_log(
    conn: sqlite3.Connection,
    log: CalculatedWorkoutLog,
) -> CalculatedWorkoutLog:
    """Overwrite a stored workout log."""
    if log.id is None:
        raise ValueError("Pro aktualizaci musí mít záznam své id.")

    cursor = conn.execute(
        """
        UPDATE workout_logs
        SET workout_type = ?, duration_minutes = ?, notes = ?
        WHERE id = ?
        """,
        (log.workout_type, log.duration_minutes, log.notes, log.id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise LookupError(f"Záznam tréninku {log.id} nebyl nalezen.")
    return log


def delete_food_log(conn: sqlite3.Connection, log_id: int) -> bool:
    """Delete a single food log. Returns True when a row was removed."""
    cursor = conn.execute("DELETE FROM food_logs WHERE id = ?", (log_id,))
    conn.commit()
    return cursor.rowcount > 0


def delete_weight_log(conn: sqlite3.Connection, log_id: int) -> bool:
    """Delete a single weight log. Returns True when a row was removed."""
    cursor = conn.execute("DELETE FROM weight_logs WHERE id = ?", (log_id,))
    conn.commit()
    return cursor.rowcount > 0


def delete_workout_log(conn: sqlite3.Connection, log_id: int) -> bool:
    """Delete a single workout log. Returns True when a row was removed."""
    cursor = conn.execute("DELETE FROM workout_logs WHERE id = ?", (log_id,))
    conn.commit()
    return cursor.rowcount > 0
