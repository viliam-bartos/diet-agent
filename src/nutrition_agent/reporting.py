"""Reporting, aggregation, and visual chart generation across multiple timeframes."""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from nutrition_agent.repository import (
    get_food_logs_in_range,
    get_user_profile,
    get_weight_logs_in_range,
)
from nutrition_agent.targets import DailyTargets, calculate_daily_targets


@dataclass(frozen=True)
class AggregatedNutrition:
    """Aggregated nutritional totals for a given set of food logs."""

    calories: float
    protein_usable: float
    protein_raw: float
    carbs: float
    fats: float
    days_count: int


def render_progress_bar(current: float, target: float, width: int = 20) -> str:
    """Render a clean text progress bar [####----] without emoji."""
    if target <= 0:
        return f"[{'-' * width}]   0.0%"

    ratio = min(max(current / target, 0.0), 2.0)
    filled_len = int(round(ratio * width))
    bar_fill = min(filled_len, width)
    bar_empty = width - bar_fill
    bar = "#" * bar_fill + "-" * bar_empty
    pct = (current / target) * 100.0
    return f"[{bar}] {pct:5.1f}%"


def render_bar_chart(
    items: list[tuple[str, float, str]],
    width: int = 22,
) -> list[str]:
    """Render a horizontal bar chart for daily or periodic values."""
    if not items:
        return []

    max_val = max((val for _, val, _ in items), default=1.0)
    if max_val <= 0:
        max_val = 1.0

    lines: list[str] = []
    for label, val, unit in items:
        ratio = min(max(val / max_val, 0.0), 1.0)
        filled = int(round(ratio * width))
        empty = width - filled
        bar = "#" * filled + "-" * empty
        lines.append(f"{label} | {bar} {val:6.1f} {unit}")
    return lines


def format_daily_target_breakdown(
    consumed_calories: float,
    consumed_protein: float,
    consumed_carbs: float,
    consumed_fats: float,
    targets: DailyTargets,
) -> str:
    """Format table and progress bars comparing daily consumption with targets."""
    cal_diff = consumed_calories - targets.calories
    cal_diff_str = f"+{cal_diff:.1f}" if cal_diff >= 0 else f"{cal_diff:.1f}"

    prot_diff = consumed_protein - targets.protein_usable
    prot_diff_str = f"+{prot_diff:.1f}" if prot_diff >= 0 else f"{prot_diff:.1f}"

    carbs_diff = consumed_carbs - targets.carbs
    carbs_diff_str = f"+{carbs_diff:.1f}" if carbs_diff >= 0 else f"{carbs_diff:.1f}"

    fats_diff = consumed_fats - targets.fats
    fats_diff_str = f"+{fats_diff:.1f}" if fats_diff >= 0 else f"{fats_diff:.1f}"

    bar_cal = render_progress_bar(consumed_calories, targets.calories, width=16)
    bar_prot = render_progress_bar(consumed_protein, targets.protein_usable, width=16)
    bar_carbs = render_progress_bar(consumed_carbs, targets.carbs, width=16)
    bar_fats = render_progress_bar(consumed_fats, targets.fats, width=16)

    lines = [
        f"Cílový stav: {targets.goal.value.upper()} | Typ dne: {targets.day_type.value.upper()} "
        f"| Váha: {targets.weight_kg:.1f} kg",
        "",
        "Přehled plnění denního cíle:",
        f"Energie:     {bar_cal}  {consumed_calories:6.1f} / {targets.calories:6.1f} kcal "
        f"(rozdíl: {cal_diff_str} kcal)",
        f"Bílkoviny:   {bar_prot}  {consumed_protein:6.1f} / {targets.protein_usable:6.1f} g DIAAS "
        f"(rozdíl: {prot_diff_str} g)",
        f"Sacharidy:   {bar_carbs}  {consumed_carbs:6.1f} / {targets.carbs:6.1f} g "
        f"(rozdíl: {carbs_diff_str} g)",
        f"Tuky:        {bar_fats}  {consumed_fats:6.1f} / {targets.fats:6.1f} g "
        f"(rozdíl: {fats_diff_str} g)",
    ]
    return "\n".join(lines)


def get_period_boundaries(
    period: str,
    ref_date: datetime | None = None,
) -> tuple[str | None, str | None, str]:
    """Calculate start_iso, end_iso, and human title for given timeframe."""
    now = ref_date or datetime.now()
    now_str = now.strftime("%Y-%m-%d")

    p = period.strip().lower()
    if p in ("denni", "den", "today", "dnes"):
        return now_str, now_str, f"Denní souhrn ({now_str})"

    if p in ("tydenni", "tyden", "week", "7d", "7"):
        start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
        return start, now_str, f"Týdenní souhrn ({start} až {now_str})"

    if p in ("mesicni", "mesic", "month", "30d", "30"):
        start = (now - timedelta(days=29)).strftime("%Y-%m-%d")
        return start, now_str, f"Měsíční souhrn ({start} až {now_str})"

    if p in ("pulrocni", "pulrok", "half_year", "6m", "180d", "180"):
        start = (now - timedelta(days=179)).strftime("%Y-%m-%d")
        return start, now_str, f"Půlroční souhrn ({start} až {now_str})"

    if p in ("rocni", "rok", "year", "1y", "365d", "365"):
        start = (now - timedelta(days=364)).strftime("%Y-%m-%d")
        return start, now_str, f"Roční souhrn ({start} až {now_str})"

    if p in ("kompletni", "vsechno", "vse", "all", "total"):
        return None, None, "Kompletní souhrn (všechna data)"

    raise ValueError(
        f"Neznámé období: '{period}'. "
        "Možnosti: denni, tydenni, mesicni, pulrocni, rocni, kompletni."
    )


def generate_period_report(
    conn: sqlite3.Connection,
    period: str,
    ref_date: datetime | None = None,
) -> str:
    """Generate comprehensive summary report with tables and text charts."""
    start_iso, end_iso, title = get_period_boundaries(period, ref_date)

    start_query = f"{start_iso}T00:00:00" if start_iso else None
    end_query = f"{end_iso}T23:59:59" if end_iso else None

    food_logs = get_food_logs_in_range(conn, start_query, end_query)
    weight_logs = get_weight_logs_in_range(conn, start_query, end_query)

    goal, day_type, current_weight = get_user_profile(conn)
    targets = calculate_daily_targets(current_weight, goal, day_type)

    lines: list[str] = [f"=== {title} ===", ""]

    if not food_logs and not weight_logs:
        lines.append("Za vybrané období nebyly nalezeny žádné záznamy.")
        return "\n".join(lines)

    # 1. Single day breakdown
    if period.strip().lower() in ("denni", "den", "today", "dnes"):
        total_cal = sum(item.calories for item in food_logs)
        total_prot = sum(item.protein_usable for item in food_logs)
        total_carbs = sum(item.carbs for item in food_logs)
        total_fats = sum(item.fats for item in food_logs)

        lines.append(
            format_daily_target_breakdown(total_cal, total_prot, total_carbs, total_fats, targets)
        )

        if food_logs:
            lines.append("")
            lines.append(f"Zaznamenaná jídla ({len(food_logs)}):")
            for item in food_logs:
                time_part = item.logged_at[11:16] if len(item.logged_at) >= 16 else ""
                prefix = f"- [{time_part}] " if time_part else "- "
                state_flag = f" ({item.state})" if item.state == "cooked" else ""
                lines.append(
                    f"{prefix}{item.food_name_matched} ({item.amount_g:.0f} g{state_flag}): "
                    f"{item.calories:.1f} kcal | B: {item.protein_usable:.1f} g DIAAS | "
                    f"S: {item.carbs:.1f} g | T: {item.fats:.1f} g"
                )

        if weight_logs:
            lines.append("")
            lines.append(f"Zaznamenaná hmotnost: {weight_logs[-1].weight_kg:.1f} kg")

        return "\n".join(lines)

    # 2. Multi-day aggregation
    daily_cals: dict[str, float] = defaultdict(float)
    daily_prots: dict[str, float] = defaultdict(float)
    daily_carbs: dict[str, float] = defaultdict(float)
    daily_fats: dict[str, float] = defaultdict(float)

    for log in food_logs:
        day_str = log.logged_at[:10]
        daily_cals[day_str] += log.calories
        daily_prots[day_str] += log.protein_usable
        daily_carbs[day_str] += log.carbs
        daily_fats[day_str] += log.fats

    logged_days_count = len(daily_cals)
    total_cal = sum(daily_cals.values())
    total_prot = sum(daily_prots.values())
    total_carbs = sum(daily_carbs.values())
    total_fats = sum(daily_fats.values())

    avg_cal = (total_cal / logged_days_count) if logged_days_count > 0 else 0.0
    avg_prot = (total_prot / logged_days_count) if logged_days_count > 0 else 0.0
    avg_carbs = (total_carbs / logged_days_count) if logged_days_count > 0 else 0.0
    avg_fats = (total_fats / logged_days_count) if logged_days_count > 0 else 0.0

    lines.append(
        f"Aktivní cíl: {goal.value.upper()} | Denní kalorický cíl: {targets.calories:.0f} kcal "
        f"({targets.day_type.value})"
    )
    lines.append(f"Počet dnů se záznamem stravy: {logged_days_count}")
    lines.append(f"Celkem přijato: {total_cal:.0f} kcal ({total_prot:.1f} g DIAAS bílkovin)")
    lines.append("")
    lines.append("Průměrné denní hodnoty vs cíl:")
    lines.append(
        f"- Energie:     {avg_cal:6.1f} / {targets.calories:6.1f} kcal "
        f"({(avg_cal / targets.calories * 100.0) if targets.calories else 0:.1f} %)"
    )
    lines.append(
        f"- Bílkoviny:   {avg_prot:6.1f} / {targets.protein_usable:6.1f} g DIAAS "
        f"({(avg_prot / targets.protein_usable * 100.0) if targets.protein_usable else 0:.1f} %)"
    )
    lines.append(
        f"- Sacharidy:   {avg_carbs:6.1f} / {targets.carbs:6.1f} g "
        f"({(avg_carbs / targets.carbs * 100.0) if targets.carbs else 0:.1f} %)"
    )
    lines.append(
        f"- Tuky:        {avg_fats:6.1f} / {targets.fats:6.1f} g "
        f"({(avg_fats / targets.fats * 100.0) if targets.fats else 0:.1f} %)"
    )

    # Calories bar chart
    if daily_cals:
        lines.append("")
        lines.append("Graf přijaté energie po dnech:")
        # If period has many days, show last 14 or aggregated
        sorted_days = sorted(daily_cals.keys())
        days_to_display = sorted_days[-14:] if len(sorted_days) > 14 else sorted_days
        chart_data = [(day, daily_cals[day], "kcal") for day in days_to_display]
        lines.extend(render_bar_chart(chart_data, width=20))
        if len(sorted_days) > 14:
            lines.append(f"(Zobrazeno posledních 14 dnů z celkových {len(sorted_days)})")

    # Weight tracking
    if weight_logs:
        lines.append("")
        lines.append("Statistika tělesné hmotnosti:")
        weights = [w.weight_kg for w in weight_logs]
        w_start = weights[0]
        w_end = weights[-1]
        w_diff = w_end - w_start
        w_diff_str = f"+{w_diff:.1f}" if w_diff >= 0 else f"{w_diff:.1f}"
        lines.append(f"- Počáteční váha: {w_start:.1f} kg")
        lines.append(f"- Poslední váha:   {w_end:.1f} kg")
        lines.append(
            f"- Rozdíl za období: {w_diff_str} kg "
            f"(min: {min(weights):.1f} kg, max: {max(weights):.1f} kg)"
        )
        lines.append("")
        lines.append("Průběh vážení:")
        weight_chart = [(w.logged_at[:10], w.weight_kg, "kg") for w in weight_logs[-10:]]
        lines.extend(render_bar_chart(weight_chart, width=16))

    return "\n".join(lines)
