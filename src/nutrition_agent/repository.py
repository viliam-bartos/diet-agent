"""Data access repository for foods and logs."""

import json
import sqlite3
from pathlib import Path

from nutrition_agent.models import (
    CalculatedFoodLog,
    CalculatedWeightLog,
    CalculatedWorkoutLog,
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
                   calories, protein_raw, protein_usable, carbs, fats
            FROM food_logs
            ORDER BY logged_at DESC
            """
        )
        for row in cursor.fetchall():
            food_desc = f"{row['food_name_matched']} ({row['amount_g']:.0f} g, {row['state']})"
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
                    "duration_minutes": row["duration_minutes"],
                }
            )

    records.sort(key=lambda r: str(r["logged_at"]), reverse=True)
    total_count = len(records)
    paginated = records[offset : offset + limit]
    return paginated, total_count
