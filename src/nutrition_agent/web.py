"""FastAPI web server and REST API for Nutrition Agent dashboard."""

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from nutrition_agent.cli import handle_user_message
from nutrition_agent.config import settings
from nutrition_agent.db import get_connection, init_db
from nutrition_agent.repository import (
    get_dashboard_kpis,
    get_heatmap_data,
    get_unified_records,
    get_user_profile,
    get_weight_trend,
    load_foods_from_json,
)
from nutrition_agent.targets import calculate_daily_targets

TEMPLATES_DIR = Path(__file__).parent / "templates"


class MessagePayload(BaseModel):
    """Incoming user message payload."""

    message: str


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

    @app.post("/api/message")
    async def api_message(payload: MessagePayload):
        init_db(settings.database_path)
        conn = get_connection(settings.database_path)
        foods = load_foods_from_json(settings.foods_data_path)
        try:
            reply = handle_user_message(payload.message, foods, conn)
            return {"reply": reply}
        finally:
            conn.close()

    return app


def run_server(host: str | None = None, port: int | None = None) -> None:
    """Run Uvicorn ASGI server."""
    import uvicorn

    server_host = host or settings.web_host
    server_port = port or settings.web_port
    app = create_app()
    uvicorn.run(app, host=server_host, port=server_port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    run_server()
