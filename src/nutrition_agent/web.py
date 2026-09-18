"""FastAPI web server and REST API for Nutrition Agent dashboard."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from nutrition_agent.cli import handle_user_message
from nutrition_agent.config import settings
from nutrition_agent.db import get_connection, init_db
from nutrition_agent.models import (
    MAX_WEIGHT_KG,
    MIN_WEIGHT_KG,
    CalculatedWorkoutLog,
)
from nutrition_agent.nutrition import recalculate_food_log, validate_body_weight
from nutrition_agent.repository import (
    delete_food_log,
    delete_weight_log,
    delete_workout_log,
    ensure_food_catalog,
    get_dashboard_kpis,
    get_food,
    get_food_log,
    get_heatmap_data,
    get_unified_records,
    get_user_profile,
    get_weight_log,
    get_weight_trend,
    get_workout_log,
    load_foods,
    update_food_log,
    update_weight_log,
    update_workout_log,
)
from nutrition_agent.targets import calculate_daily_targets

TEMPLATES_DIR = Path(__file__).parent / "templates"

RecordType = Literal["food", "weight", "workout"]


class MessagePayload(BaseModel):
    """Incoming user message payload."""

    message: str


class FoodLogEdit(BaseModel):
    """Editable fields of a stored food log."""

    amount_g: float
    state: Literal["raw", "cooked"] = "raw"

    @field_validator("amount_g")
    @classmethod
    def validate_amount(cls, value: float) -> float:
        """Grams must be positive (Fail Fast)."""
        if value <= 0:
            raise ValueError("Hmotnost musí být kladné číslo.")
        return value


class WeightLogEdit(BaseModel):
    """Editable fields of a stored weight log."""

    weight_kg: float

    @field_validator("weight_kg")
    @classmethod
    def validate_weight(cls, value: float) -> float:
        """Weight must stay within the physiological range (Fail Fast)."""
        try:
            return validate_body_weight(value)
        except ValueError as exc:
            raise ValueError(f"Povolený rozsah je {MIN_WEIGHT_KG} až {MAX_WEIGHT_KG} kg.") from exc


class WorkoutLogEdit(BaseModel):
    """Editable fields of a stored workout log."""

    workout_type: str = Field(min_length=1)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    notes: str | None = None


def _resolve_period_dates(period: str) -> tuple[str | None, str | None]:
    """Convert period shorthand into start_iso and end_iso."""
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d")

    p = period.lower().strip()
    if p in ("7d", "7", "tyden", "tydenni", "week"):
        start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
        return f"{start}T00:00:00", f"{now_str}T23:59:59"
    if p in ("30d", "30", "mesic", "mesicni", "month"):
        start = (now - timedelta(days=29)).strftime("%Y-%m-%d")
        return f"{start}T00:00:00", f"{now_str}T23:59:59"
    if p in ("90d", "90", "3m", "3_mesice"):
        start = (now - timedelta(days=89)).strftime("%Y-%m-%d")
        return f"{start}T00:00:00", f"{now_str}T23:59:59"
    if p in ("180d", "180", "6m", "pulrok", "pulrocni"):
        start = (now - timedelta(days=179)).strftime("%Y-%m-%d")
        return f"{start}T00:00:00", f"{now_str}T23:59:59"
    if p in ("365d", "365", "1y", "rok", "rocni", "year"):
        start = (now - timedelta(days=364)).strftime("%Y-%m-%d")
        return f"{start}T00:00:00", f"{now_str}T23:59:59"
    if p in ("all", "vse", "kompletni"):
        return None, None

    # Default to 30 days
    start = (now - timedelta(days=29)).strftime("%Y-%m-%d")
    return f"{start}T00:00:00", f"{now_str}T23:59:59"


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    # Schema creation is idempotent and upgrades databases created by older versions.
    init_db(settings.database_path)

    app = FastAPI(title="Nutrition Agent Dashboard")

    @app.get("/", response_class=HTMLResponse)
    async def get_dashboard():
        template_path = TEMPLATES_DIR / "dashboard.html"
        if not template_path.exists():
            return HTMLResponse("<h1>Šablona dashboard.html nebyla nalezena.</h1>", status_code=404)
        return HTMLResponse(template_path.read_text(encoding="utf-8"))

    @app.get("/api/kpis")
    async def api_kpis(period: str = Query("30d")):
        start_iso, end_iso = _resolve_period_dates(period)
        conn = get_connection(settings.database_path)
        try:
            kpis = get_dashboard_kpis(conn, start_iso, end_iso)
            goal, day_type, weight = get_user_profile(conn)
            targets = calculate_daily_targets(weight, goal, day_type)
            kpis["goal"] = goal.value
            kpis["day_type"] = day_type.value
            kpis["target_calories"] = (
                settings.target_calories if settings.target_calories else targets.calories
            )
            kpis["target_protein"] = (
                settings.target_protein_g if settings.target_protein_g else targets.protein_usable
            )
            return kpis
        finally:
            conn.close()

    @app.get("/api/weight-trend")
    async def api_weight_trend(period: str = Query("30d")):
        start_iso, end_iso = _resolve_period_dates(period)
        conn = get_connection(settings.database_path)
        try:
            return get_weight_trend(conn, start_iso, end_iso)
        finally:
            conn.close()

    @app.get("/api/heatmap")
    async def api_heatmap(period: str = Query("30d")):
        start_iso, end_iso = _resolve_period_dates(period)
        conn = get_connection(settings.database_path)
        try:
            goal, day_type, weight = get_user_profile(conn)
            targets = calculate_daily_targets(weight, goal, day_type)
            target_cal = settings.target_calories if settings.target_calories else targets.calories
            target_prot = (
                settings.target_protein_g if settings.target_protein_g else targets.protein_usable
            )
            return get_heatmap_data(
                conn,
                start_iso,
                end_iso,
                target_calories=target_cal,
                target_protein=target_prot,
            )
        finally:
            conn.close()

    @app.get("/api/records")
    async def api_records(
        type: str = Query("all"),
        limit: int = Query(15, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        conn = get_connection(settings.database_path)
        try:
            items, total = get_unified_records(conn, record_type=type, limit=limit, offset=offset)
            return {"items": items, "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()

    @app.patch("/api/records/{record_type}/{record_id}")
    async def api_update_record(
        record_type: RecordType,
        record_id: int,
        payload: dict,
    ):
        conn = get_connection(settings.database_path)
        try:
            try:
                if record_type == "food":
                    food_edit = FoodLogEdit.model_validate(payload)
                    return _update_food_record(conn, record_id, food_edit)
                if record_type == "weight":
                    weight_edit = WeightLogEdit.model_validate(payload)
                    return _update_weight_record(conn, record_id, weight_edit)
                workout_edit = WorkoutLogEdit.model_validate(payload)
                return _update_workout_record(conn, record_id, workout_edit)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=_first_error_message(exc),
                ) from exc
        finally:
            conn.close()

    @app.delete("/api/records/{record_type}/{record_id}")
    async def api_delete_record(record_type: RecordType, record_id: int):
        conn = get_connection(settings.database_path)
        try:
            deleters = {
                "food": delete_food_log,
                "weight": delete_weight_log,
                "workout": delete_workout_log,
            }
            if not deleters[record_type](conn, record_id):
                raise HTTPException(status_code=404, detail="Záznam nebyl nalezen.")
            return {"deleted": True, "type": record_type, "id": record_id}
        finally:
            conn.close()

    @app.post("/api/message")
    async def api_message(payload: MessagePayload):
        conn = get_connection(settings.database_path)
        ensure_food_catalog(conn, settings.foods_data_path)
        foods = load_foods(conn)
        try:
            reply = handle_user_message(payload.message, foods, conn)
            return {"reply": reply}
        finally:
            conn.close()

    return app


def _first_error_message(error: ValidationError) -> str:
    """Turn a Pydantic validation error into one readable sentence."""
    first = error.errors()[0]
    field = ".".join(str(part) for part in first.get("loc", ())) or "vstup"
    return f"Neplatná hodnota pole '{field}': {first.get('msg', 'chyba validace')}"


def _update_food_record(conn, record_id: int, edit: FoodLogEdit) -> dict[str, object]:
    """Recompute and store an edited food log."""
    existing = get_food_log(conn, record_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Záznam nebyl nalezen.")

    food_def = get_food(conn, existing.food_name_matched)
    if food_def is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Potravina '{existing.food_name_matched}' už není v katalogu, "
                "takže nelze přepočítat makra."
            ),
        )

    updated = recalculate_food_log(existing, food_def, edit.amount_g, edit.state)
    update_food_log(conn, updated)
    return {
        "id": updated.id,
        "amount_g": updated.amount_g,
        "state": updated.state,
        "calories": updated.calories,
        "protein_usable": updated.protein_usable,
        "carbs": updated.carbs,
        "fats": updated.fats,
    }


def _update_weight_record(conn, record_id: int, edit: WeightLogEdit) -> dict[str, object]:
    """Store an edited weight log."""
    existing = get_weight_log(conn, record_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Záznam nebyl nalezen.")

    updated = existing.model_copy(update={"weight_kg": edit.weight_kg})
    update_weight_log(conn, updated)
    return {"id": updated.id, "weight_kg": updated.weight_kg}


def _update_workout_record(
    conn,
    record_id: int,
    edit: WorkoutLogEdit,
) -> dict[str, object]:
    """Store an edited workout log."""
    existing = get_workout_log(conn, record_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Záznam nebyl nalezen.")

    updated = CalculatedWorkoutLog(
        id=existing.id,
        logged_at=existing.logged_at,
        raw_input=existing.raw_input,
        workout_type=edit.workout_type,
        duration_minutes=edit.duration_minutes,
        notes=edit.notes,
    )
    update_workout_log(conn, updated)
    return {
        "id": updated.id,
        "workout_type": updated.workout_type,
        "duration_minutes": updated.duration_minutes,
        "notes": updated.notes,
    }


def run_server(host: str | None = None, port: int | None = None) -> None:
    """Run Uvicorn ASGI server."""
    import uvicorn

    server_host = host or settings.web_host
    server_port = port or settings.web_port
    app = create_app()
    uvicorn.run(app, host=server_host, port=server_port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    run_server()
