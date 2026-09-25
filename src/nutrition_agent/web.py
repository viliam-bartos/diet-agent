"""FastAPI web server and REST API for Nutrition Agent dashboard."""

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from nutrition_agent import __version__
from nutrition_agent.cli import handle_user_message
from nutrition_agent.config import settings
from nutrition_agent.db import get_connection, init_db
from nutrition_agent.models import (
    MAX_WEIGHT_KG,
    MIN_WEIGHT_KG,
    CalculatedFoodLog,
    CalculatedWorkoutLog,
    FoodDefinition,
    parse_date_argument,
)
from nutrition_agent.nutrition import (
    apply_manual_macros,
    complete_manual_macros,
    per_100g_from_portion,
    per_portion_from_100g,
    recalculate_food_log,
    validate_body_weight,
)
from nutrition_agent.repository import (
    delete_food_log,
    delete_weight_log,
    delete_workout_log,
    ensure_food_catalog,
    get_day_detail,
    get_food,
    get_food_log,
    get_heatmap_for_days,
    get_unified_records,
    get_user_profile,
    get_week_summary,
    get_weight_log,
    get_weight_trend_for_days,
    get_workout_log,
    load_foods,
    update_food_log,
    update_food_nutrition,
    update_weight_log,
    update_workout_log,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Bumped whenever the API changes shape. The dashboard carries the same number as a
# literal in its template and compares it with the value the server reports. A process
# started before the change serves the new template from disk while still running the
# old code, and the version string alone does not give that away.
API_LEVEL: int = 4

RecordType = Literal["food", "weight", "workout"]


class MessagePayload(BaseModel):
    """Incoming user message payload."""

    message: str
    date: str = Field(
        default="",
        description="Datum, na které se má záznam zapsat; prázdné znamená dnes",
    )


class FoodLogEdit(BaseModel):
    """Editable fields of a stored food log.

    Macros are typed per 100 g of the raw food, the same basis the catalog uses, so the
    numbers can be compared with a package label and written to the catalog unchanged.
    Without them the log is recomputed from the catalog. One of the four may be left out
    and is derived from the energy balance.
    """

    amount_g: float
    state: Literal["raw", "cooked"] = "raw"
    calories_per_100g: float | None = None
    protein_usable_per_100g: float | None = None
    carbs_per_100g: float | None = None
    fats_per_100g: float | None = None
    alcohol_per_100g: float | None = None
    update_catalog: bool = False

    @field_validator("amount_g")
    @classmethod
    def validate_amount(cls, value: float) -> float:
        """Grams must be positive (Fail Fast)."""
        if value <= 0:
            raise ValueError("Hmotnost musí být kladné číslo.")
        return value

    @field_validator(
        "calories_per_100g",
        "protein_usable_per_100g",
        "carbs_per_100g",
        "fats_per_100g",
        "alcohol_per_100g",
    )
    @classmethod
    def validate_non_negative(cls, value: float | None) -> float | None:
        """Nutrition values are never negative (Fail Fast)."""
        if value is not None and value < 0:
            raise ValueError("Nutriční hodnota nesmí být záporná.")
        return value

    @model_validator(mode="after")
    def validate_manual_macros(self) -> "FoodLogEdit":
        """Keep exactly one macro derivable, and never store an empty one."""
        typed = [value for value in self.macros if value is not None]
        if typed and len(typed) < 3:
            raise ValueError("Zadej aspoň tři hodnoty ze čtyř, tu chybějící dopočítám.")
        if len(typed) >= 3 and all(value == 0 for value in typed):
            raise ValueError("Ruční korekce nesmí vynulovat kalorie i všechna makra.")
        if self.update_catalog and not typed:
            raise ValueError("Do katalogu lze uložit jen ručně zadaná makra.")
        return self

    @property
    def macros(self) -> tuple[float | None, float | None, float | None, float | None]:
        """The four energy-carrying numbers of the payload, per 100 g."""
        return (
            self.calories_per_100g,
            self.protein_usable_per_100g,
            self.carbs_per_100g,
            self.fats_per_100g,
        )

    @property
    def macros_provided(self) -> bool:
        """Report whether the payload carries manually typed macros."""
        return any(value is not None for value in self.macros)


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


def _parse_day_or_422(value: str) -> str:
    """Resolve a day query parameter, turning a bad format into a 422."""
    try:
        return parse_date_argument(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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

        page = template_path.read_text(encoding="utf-8").replace("{{APP_VERSION}}", __version__)
        # The dashboard must never be served from a stale browser cache.
        return HTMLResponse(page, headers={"Cache-Control": "no-store, must-revalidate"})

    @app.get("/api/version")
    async def api_version():
        return {"version": __version__, "api_level": API_LEVEL}

    @app.get("/api/day")
    async def api_day(date: str = Query("", description="YYYY-MM-DD, prázdné znamená dnes")):
        day = _parse_day_or_422(date)
        conn = get_connection(settings.database_path)
        try:
            detail = get_day_detail(conn, day)
            goal, _, weight = get_user_profile(conn)
            detail["goal"] = goal.value
            detail["weight_kg"] = weight
            detail["target_calories"] = (
                settings.target_calories if settings.target_calories else detail["target_calories"]
            )
            detail["target_protein"] = (
                settings.target_protein_g if settings.target_protein_g else detail["target_protein"]
            )
            detail["version"] = __version__
            detail["api_level"] = API_LEVEL
            return detail
        finally:
            conn.close()

    @app.get("/api/week")
    async def api_week(date: str = Query("", description="Poslední den okna, prázdné je dnes")):
        end_day = _parse_day_or_422(date)
        conn = get_connection(settings.database_path)
        try:
            return get_week_summary(conn, end_day, days=7)
        finally:
            conn.close()

    @app.get("/api/weight-trend")
    async def api_weight_trend(days: int = Query(365, ge=1, le=3650)):
        conn = get_connection(settings.database_path)
        try:
            return get_weight_trend_for_days(conn, days)
        finally:
            conn.close()

    @app.get("/api/heatmap")
    async def api_heatmap(days: int = Query(365, ge=1, le=3650)):
        conn = get_connection(settings.database_path)
        try:
            return get_heatmap_for_days(conn, days)
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
            target_day = _parse_day_or_422(payload.date) if payload.date.strip() else None
            reply = handle_user_message(payload.message, foods, conn, target_day=target_day)
            return {"reply": reply, "date": target_day}
        finally:
            conn.close()

    return app


def _first_error_message(error: ValidationError) -> str:
    """Turn a Pydantic validation error into one readable sentence."""
    first = error.errors()[0]
    field = ".".join(str(part) for part in first.get("loc", ())) or "vstup"
    return f"Neplatná hodnota pole '{field}': {first.get('msg', 'chyba validace')}"


def _update_food_record(conn, record_id: int, edit: FoodLogEdit) -> dict[str, object]:
    """Store an edited food log, either recomputed or with the typed macros."""
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

    if edit.macros_provided:
        stored_alcohol_per_100g = per_100g_from_portion(
            existing.alcohol, existing.amount_g, existing.state, food_def.name
        )
        alcohol_per_100g = (
            stored_alcohol_per_100g if edit.alcohol_per_100g is None else edit.alcohol_per_100g
        )

        try:
            per_100g = complete_manual_macros(
                edit.calories_per_100g,
                edit.protein_usable_per_100g,
                edit.carbs_per_100g,
                edit.fats_per_100g,
                alcohol=alcohol_per_100g,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        def per_portion(value: float) -> float:
            return per_portion_from_100g(value, edit.amount_g, edit.state, food_def.name)

        updated = apply_manual_macros(
            existing,
            food_def,
            edit.amount_g,
            edit.state,
            calories=per_portion(per_100g.calories),
            protein_usable=per_portion(per_100g.protein_usable),
            carbs=per_portion(per_100g.carbs),
            fats=per_portion(per_100g.fats),
            alcohol=per_portion(alcohol_per_100g),
        )
    else:
        updated = recalculate_food_log(existing, food_def, edit.amount_g, edit.state)

    update_food_log(conn, updated)

    if edit.update_catalog:
        _store_catalog_correction(conn, food_def, updated)

    return {
        "id": updated.id,
        "amount_g": updated.amount_g,
        "state": updated.state,
        "calories": updated.calories,
        "protein_usable": updated.protein_usable,
        "carbs": updated.carbs,
        "fats": updated.fats,
        "nutrition_source": updated.nutrition_source,
        "catalog_updated": edit.update_catalog,
    }


def _store_catalog_correction(
    conn: sqlite3.Connection,
    food_def: FoodDefinition,
    log: CalculatedFoodLog,
) -> None:
    """Write the corrected log values back to the catalog, on its per-100 g basis.

    The log stores the portion, the catalog the raw 100 g, so the values are divided by
    the same raw equivalent the portion was scaled with.
    """

    def per_100g(value: float) -> float:
        return per_100g_from_portion(value, log.amount_g, log.state, food_def.name)

    update_food_nutrition(
        conn,
        food_def.name,
        calories=per_100g(log.calories),
        protein=per_100g(log.protein_raw),
        carbs=per_100g(log.carbs),
        fats=per_100g(log.fats),
        alcohol=per_100g(log.alcohol),
    )


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
