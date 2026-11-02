"""Energy balance against maintenance and the body mass it explains.

The comparison needs a maintenance baseline and an energy cost for the tissue that is
gained or lost. Both are estimates, so the numbers are constants with the reasoning
next to them rather than magic values inside a function.

Sources behind the constants:
- adipose tissue carries about 7700 kcal per kg (the classic Wishnofsky rule),
- gaining a kilo of skeletal muscle (including the metabolic cost of protein synthesis,
  not just the ~1800 kcal chemical energy content of wet muscle) requires ~3500 kcal,
- in a non-beginner lifter, lean mass and fat mass are gained in roughly a 1:1 (50:50)
  ratio (~5600 kcal/kg of mixed gain) up to the physiological muscle gain ceiling of
  ~0.50 kg per 30 days; any surplus beyond that cap is stored as adipose tissue.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from nutrition_agent.models import local_date, utc_now_iso
from nutrition_agent.repository import (
    get_calorie_offset,
    get_day_summaries,
    get_user_profile,
    get_weight_logs_between_days,
    get_weight_logs_in_range,
    set_calorie_offset,
    set_user_setting,
)
from nutrition_agent.targets import DayType, DietGoal, calculate_daily_targets

FAT_KCAL_PER_KG: float = 7700.0
LEAN_KCAL_PER_KG: float = 3500.0
LEAN_SHARE_OF_GAIN: float = 0.50
MAX_LEAN_GAIN_KG_PER_30_DAYS: float = 0.50
# Losses come mostly from fat, so a deficit is converted with the adipose energy density.
LOSS_KCAL_PER_KG: float = FAT_KCAL_PER_KG

# Tissue energy density for weight gain during calibration:
# In resistance-trained individuals, mixed tissue gain (muscle, fat, glycogen, bound water,
# expanded blood volume) carries ~6500 to 7200 kcal/kg (approx 6850 kcal/kg, meaning ~8200 kcal
# per 1.20 kg gained).
GAIN_KCAL_PER_KG: float = 6850.0

# Below this share of logged days the balance is a guess, not a measurement.
MIN_LOGGED_SHARE: float = 0.70

# Minimum weigh-ins required in a period to calibrate the maintenance baseline.
MIN_CALIBRATION_WEIGH_INS: int = 2


@dataclass(frozen=True)
class MassEstimate:
    """What a given energy surplus or deficit should mean for body mass."""

    kilograms: float
    lean_kilograms: float
    fat_kilograms: float
    kcal_per_kg: float
    gain: bool


def energy_per_kg_of_gain() -> float:
    """Return the energy needed for one kilo of mixed tissue (50:50 muscle:fat)."""
    return round(
        LEAN_SHARE_OF_GAIN * LEAN_KCAL_PER_KG + (1.0 - LEAN_SHARE_OF_GAIN) * FAT_KCAL_PER_KG, 1
    )


def estimate_mass_change(energy_balance_kcal: float, days: int = 30) -> MassEstimate:
    """Convert an energy surplus (or deficit) into the mass it explains.

    A surplus is split 50:50 between lean and fat mass up to the physiological muscle
    gain ceiling (~0.50 kg per 30 days); any surplus above that cap goes into fat tissue
    at 7700 kcal/kg. A deficit is treated as mostly fat.
    """
    gain = energy_balance_kcal >= 0
    if not gain:
        kilograms = round(energy_balance_kcal / LOSS_KCAL_PER_KG, 2)
        return MassEstimate(
            kilograms=kilograms,
            lean_kilograms=0.0,
            fat_kilograms=kilograms,
            kcal_per_kg=LOSS_KCAL_PER_KG,
            gain=False,
        )

    base_kcal_per_kg = energy_per_kg_of_gain()
    effective_days = max(days, 1)
    max_lean_kg = MAX_LEAN_GAIN_KG_PER_30_DAYS * (effective_days / 30.0)
    cap_kcal = max_lean_kg * (LEAN_KCAL_PER_KG + FAT_KCAL_PER_KG)

    if energy_balance_kcal <= cap_kcal:
        raw_kg = energy_balance_kcal / base_kcal_per_kg
        lean = round(raw_kg * LEAN_SHARE_OF_GAIN, 2)
        fat = round(raw_kg - lean, 2)
        kilograms = round(lean + fat, 2)
        effective_kcal_per_kg = base_kcal_per_kg
    else:
        lean_raw = max_lean_kg
        fat_raw = max_lean_kg + (energy_balance_kcal - cap_kcal) / FAT_KCAL_PER_KG
        total_raw = lean_raw + fat_raw
        lean = round(lean_raw, 2)
        fat = round(fat_raw, 2)
        kilograms = round(lean + fat, 2)
        effective_kcal_per_kg = (
            round(energy_balance_kcal / total_raw, 1) if total_raw > 0 else base_kcal_per_kg
        )

    return MassEstimate(
        kilograms=kilograms,
        lean_kilograms=lean,
        fat_kilograms=fat,
        kcal_per_kg=effective_kcal_per_kg,
        gain=True,
    )


def energy_for_mass_change(delta_kg: float) -> float:
    """Return the tissue energy (kcal) represented by a measured scale change."""
    if delta_kg < 0:
        return delta_kg * LOSS_KCAL_PER_KG
    return delta_kg * GAIN_KCAL_PER_KG


def _daily_weights_from_logs(weights) -> list[tuple[str, float]]:
    """Collapse weight logs to one reading per local day, sorted chronologically."""
    by_day: dict[str, float] = {}
    for log in sorted(weights, key=lambda item: item.logged_at):
        by_day[local_date(log.logged_at)] = log.weight_kg
    return sorted(by_day.items(), key=lambda item: item[0])


def _regression_weight_change(
    daily_weights: list[tuple[str, float]],
) -> tuple[float, float, float, float, int]:
    """Fit a least-squares line over the weigh-ins to smooth out daily water noise.

    Returns (trend_start_kg, trend_end_kg, delta_kg, slope_kg_per_day, weigh_in_span_days).
    """
    start_dt = datetime.strptime(daily_weights[0][0], "%Y-%m-%d")
    xs = [float((datetime.strptime(day, "%Y-%m-%d") - start_dt).days) for day, _ in daily_weights]
    ys = [weight for _, weight in daily_weights]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    weigh_in_span_days = max(int(round(xs[-1])) + 1, 1)
    if denom <= 0:
        start_w = round(ys[0], 2)
        end_w = round(ys[-1], 2)
        return start_w, end_w, round(end_w - start_w, 2), 0.0, weigh_in_span_days

    raw_slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom
    intercept = mean_y - raw_slope * mean_x
    trend_start = intercept
    trend_end = intercept + raw_slope * xs[-1]
    raw_delta = trend_end - trend_start
    slope_kg_per_day = raw_delta / weigh_in_span_days
    return (
        round(trend_start, 2),
        round(trend_end, 2),
        round(raw_delta, 2),
        slope_kg_per_day,
        weigh_in_span_days,
    )


def _cumulative_surplus_slope(
    daily_weights: list[tuple[str, float]],
    daily_surplus_by_day: dict[str, float],
    fallback_daily_surplus: float,
) -> float:
    """Fit the effective daily energy surplus across the weigh-in trajectory via OLS.

    Each weigh-in day anchors the cumulative energy surplus accumulated since the first
    weigh-in, so periods with higher or lower intake are weighted along the same time axis
    as the scale regression.
    """
    if len(daily_weights) < 2:
        return fallback_daily_surplus

    start_day_str = daily_weights[0][0]
    start_dt = datetime.strptime(start_day_str, "%Y-%m-%d")
    end_dt = datetime.strptime(daily_weights[-1][0], "%Y-%m-%d")
    total_span_days = max((end_dt - start_dt).days + 1, 1)
    raw_end_x = float((end_dt - start_dt).days)
    if raw_end_x <= 0:
        return fallback_daily_surplus

    scale = total_span_days / raw_end_x
    us: list[float] = []
    es: list[float] = []

    for day_str, _ in daily_weights:
        curr_dt = datetime.strptime(day_str, "%Y-%m-%d")
        raw_x = float((curr_dt - start_dt).days)
        u = raw_x * scale
        us.append(u)
        if raw_x <= 0:
            es.append(0.0)
            continue

        days_count = int(raw_x) + 1
        cum_surplus = 0.0
        for step in range(days_count):
            d_str = (start_dt + timedelta(days=step)).strftime("%Y-%m-%d")
            cum_surplus += daily_surplus_by_day.get(d_str, fallback_daily_surplus)
        avg_surplus_to_date = cum_surplus / days_count
        es.append(avg_surplus_to_date * u)

    n = len(us)
    mean_u = sum(us) / n
    mean_e = sum(es) / n
    denom = sum((u - mean_u) ** 2 for u in us)
    if denom <= 0:
        return fallback_daily_surplus

    return sum((u - mean_u) * (e - mean_e) for u, e in zip(us, es, strict=True)) / denom


def build_maintenance_calibration(
    conn,
    start_day: str | None = None,
    end_day: str | None = None,
    days: int = 30,
    ref_date: datetime | None = None,
    min_weigh_ins: int = MIN_CALIBRATION_WEIGH_INS,
) -> dict[str, object]:
    """Compute calibrated maintenance using OLS weight slope and cumulative energy balance."""
    if not (start_day and end_day):
        start_day, end_day = _window(days, ref_date)

    summaries = get_day_summaries(conn, start_day, end_day)
    logged_days = [day for day in summaries if day["has_food"]]

    weights = get_weight_logs_between_days(conn, start_day, end_day)
    daily_weights = _daily_weights_from_logs(weights)

    calorie_offset = get_calorie_offset(conn)
    span_days = max(len(summaries), 1)

    all_weights_count = len({local_date(log.logged_at) for log in get_weight_logs_in_range(conn)})
    weigh_ins_count = len(daily_weights) or all_weights_count

    if len(daily_weights) < min_weigh_ins or not logged_days:
        return {
            "ready": False,
            "weigh_ins_count": weigh_ins_count,
            "required_weigh_ins": min_weigh_ins,
            "start_date": start_day,
            "end_date": end_day,
            "span_days": span_days,
            "weigh_in_span_days": None,
            "logged_days": len(logged_days),
            "trend_start_kg": None,
            "trend_end_kg": None,
            "delta_kg": None,
            "slope_kg_per_day": None,
            "slope_kg_per_week": None,
            "current_maintenance_kcal": None,
            "recommended_maintenance_kcal": None,
            "recommended_delta_kcal": None,
            "calorie_offset_kcal": calorie_offset,
        }

    (
        trend_start_kg,
        trend_end_kg,
        delta_kg,
        slope_kg_per_day,
        weigh_in_span_days,
    ) = _regression_weight_change(daily_weights)

    _, _, weight_kg = get_user_profile(conn)
    intake_kcal = 0.0
    maintenance_kcal = 0.0
    daily_surplus_by_day: dict[str, float] = {}
    for day in logged_days:
        day_intake = float(day["calories"])
        maintenance = calculate_daily_targets(
            weight_kg,
            DietGoal.MAINTENANCE,
            DayType(day["day_type"]),
            calorie_offset=calorie_offset,
        )
        intake_kcal += day_intake
        maintenance_kcal += maintenance.calories
        daily_surplus_by_day[str(day["date"])] = day_intake - maintenance.calories

    avg_intake_kcal = intake_kcal / len(logged_days)
    avg_maintenance_kcal = maintenance_kcal / len(logged_days)
    avg_surplus_kcal = avg_intake_kcal - avg_maintenance_kcal

    effective_daily_surplus = _cumulative_surplus_slope(
        daily_weights,
        daily_surplus_by_day,
        fallback_daily_surplus=avg_surplus_kcal,
    )
    daily_tissue_kcal = energy_for_mass_change(slope_kg_per_day)
    raw_delta_kcal = effective_daily_surplus - daily_tissue_kcal
    recommended_maintenance_kcal = round(avg_maintenance_kcal + raw_delta_kcal, 0)
    recommended_delta_kcal = round(recommended_maintenance_kcal - round(avg_maintenance_kcal, 0), 0)

    return {
        "ready": True,
        "weigh_ins_count": len(daily_weights),
        "required_weigh_ins": min_weigh_ins,
        "start_date": start_day,
        "end_date": end_day,
        "span_days": span_days,
        "weigh_in_span_days": weigh_in_span_days,
        "logged_days": len(logged_days),
        "trend_start_kg": trend_start_kg,
        "trend_end_kg": trend_end_kg,
        "delta_kg": delta_kg,
        "slope_kg_per_day": round(slope_kg_per_day, 4),
        "slope_kg_per_week": round(slope_kg_per_day * 7.0, 2),
        "avg_intake_kcal": round(avg_intake_kcal, 0),
        "current_maintenance_kcal": round(avg_maintenance_kcal, 0),
        "recommended_maintenance_kcal": recommended_maintenance_kcal,
        "recommended_delta_kcal": recommended_delta_kcal,
        "calorie_offset_kcal": calorie_offset,
    }


def apply_maintenance_calibration(
    conn,
    start_day: str | None = None,
    end_day: str | None = None,
    days: int = 30,
    ref_date: datetime | None = None,
    min_weigh_ins: int = MIN_CALIBRATION_WEIGH_INS,
) -> dict[str, object]:
    """Persist the calibrated maintenance offset and shift all goal targets accordingly."""
    calibration = build_maintenance_calibration(
        conn,
        start_day=start_day,
        end_day=end_day,
        days=days,
        ref_date=ref_date,
        min_weigh_ins=min_weigh_ins,
    )
    if not calibration["ready"]:
        raise ValueError(
            f"Pro kalibraci maintenance je potřeba alespoň {min_weigh_ins} vážení v daném období "
            f"(aktuálně {calibration['weigh_ins_count']}) a záznamy jídla."
        )

    current_offset = get_calorie_offset(conn)
    delta_kcal = float(calibration["recommended_delta_kcal"] or 0.0)
    new_offset = round(current_offset + delta_kcal, 1)
    set_calorie_offset(conn, new_offset)
    set_user_setting(conn, "maintenance_calibrated_at", str(calibration["end_date"]))

    goal, day_type, weight_kg = get_user_profile(conn)
    active_targets = calculate_daily_targets(
        weight_kg, goal, day_type, calorie_offset=new_offset
    )
    rest_maintenance = calculate_daily_targets(
        weight_kg, DietGoal.MAINTENANCE, DayType.REST, calorie_offset=new_offset
    )
    training_maintenance = calculate_daily_targets(
        weight_kg, DietGoal.MAINTENANCE, DayType.TRAINING, calorie_offset=new_offset
    )

    return {
        "applied": True,
        "applied_delta_kcal": delta_kcal,
        "calorie_offset_kcal": new_offset,
        "recommended_maintenance_kcal": calibration["recommended_maintenance_kcal"],
        "rest_maintenance_kcal": rest_maintenance.calories,
        "training_maintenance_kcal": training_maintenance.calories,
        "active_goal": goal.value,
        "active_day_type": day_type.value,
        "active_target_calories": active_targets.calories,
        "calibration": build_maintenance_calibration(
            conn,
            start_day=start_day,
            end_day=end_day,
            days=days,
            ref_date=ref_date,
            min_weigh_ins=min_weigh_ins,
        ),
    }


def _window(days: int, ref_date: datetime | None = None) -> tuple[str, str]:
    """Return the first and last local day of the finished window."""
    today = (ref_date or datetime.now()).strftime("%Y-%m-%d")
    end_day = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    start_day = (datetime.strptime(end_day, "%Y-%m-%d") - timedelta(days=days - 1)).strftime(
        "%Y-%m-%d"
    )
    return start_day, end_day


def build_balance_verdict(
    conn,
    days: int = 30,
    ref_date: datetime | None = None,
    auto_fallback: bool = True,
) -> dict[str, object]:
    """Compare the finished window against maintenance and check it against the scale.

    Only days with logged food enter the comparison, and maintenance is summed over the
    same days, so an untracked day neither creates a deficit nor hides one. The check
    against the scale needs at least two weigh-ins inside the window; without them the
    deviation is left unknown instead of being guessed.
    """
    if days < 1:
        raise ValueError("Počet dní musí být kladné číslo.")

    start_day, end_day = _window(days, ref_date)
    summaries = get_day_summaries(conn, start_day, end_day)
    logged_days = [day for day in summaries if day["has_food"]]

    fallback_applied = False
    if not logged_days and ref_date is None and auto_fallback:
        cursor = conn.execute("SELECT MAX(logged_at) as last_log FROM food_logs")
        row = cursor.fetchone()
        if row and row["last_log"]:
            last_date_str = local_date(row["last_log"])
            fallback_ref = datetime.strptime(last_date_str, "%Y-%m-%d") + timedelta(days=1)
            start_day, end_day = _window(days, fallback_ref)
            summaries = get_day_summaries(conn, start_day, end_day)
            logged_days = [day for day in summaries if day["has_food"]]
            fallback_applied = True

    goal, _, weight_kg = get_user_profile(conn)
    calorie_offset = get_calorie_offset(conn)

    intake_kcal = 0.0
    maintenance_kcal = 0.0
    for day in logged_days:
        intake_kcal += day["calories"]
        maintenance = calculate_daily_targets(
            weight_kg,
            DietGoal.MAINTENANCE,
            DayType(day["day_type"]),
            calorie_offset=calorie_offset,
        )
        maintenance_kcal += maintenance.calories

    balance_kcal = intake_kcal - maintenance_kcal
    estimate = estimate_mass_change(balance_kcal, days=len(logged_days) or days)

    weights = get_weight_logs_between_days(conn, start_day, end_day)
    weights.sort(key=lambda log: log.logged_at)
    daily_weights = _daily_weights_from_logs(weights)
    if len(daily_weights) >= 2:
        first_weight, last_weight, measured_change, _, _ = _regression_weight_change(daily_weights)
    else:
        first_weight = weights[0].weight_kg if weights else None
        last_weight = weights[-1].weight_kg if len(weights) >= 2 else None
        measured_change = (
            round(last_weight - first_weight, 2)
            if first_weight is not None and last_weight is not None
            else None
        )
    deviation = (
        round(measured_change - estimate.kilograms, 2) if measured_change is not None else None
    )
    deviation_percent = (
        round((measured_change / estimate.kilograms - 1.0) * 100.0, 1)
        if measured_change is not None and abs(estimate.kilograms) > 0.05
        else None
    )

    deviation_note = None
    if deviation is not None:
        if deviation > 0:
            deviation_note = (
                f"Váha stoupla o {abs(deviation):.2f} kg více než odpovídá kalorickému přebytku "
                "(v krátkém horizontu jde typicky o zadrženou vodu, glykogen a obsah trávení)."
            )
        elif deviation < 0:
            deviation_note = (
                f"Váha klesla o {abs(deviation):.2f} kg více než odpovídá kalorické bilanci."
            )
        else:
            deviation_note = "Změna váhy přesně odpovídá teoretickému modelu."

    logged_share = len(logged_days) / len(summaries) if summaries else 0.0
    warnings: list[str] = []
    if fallback_applied:
        warnings.append(
            f"V posledních {days} dnech nejsou záznamy, zobrazuji poslední období se záznamy "
            f"({start_day} až {end_day})."
        )
    if logged_share < MIN_LOGGED_SHARE:
        warnings.append(
            f"Zaznamenáno je {len(logged_days)} z {len(summaries)} dní, bilance je proto "
            "jen orientační."
        )
    if measured_change is None:
        warnings.append("Ve vybraném okně nejsou aspoň dvě vážení, odchylku měřit nelze.")
    if not logged_days:
        warnings.append("V tomto okně není žádný záznam jídla.")

    return {
        "start_date": start_day,
        "end_date": end_day,
        "window_days": len(summaries),
        "logged_days": len(logged_days),
        "logged_share": round(logged_share, 2),
        "goal": goal.value,
        "weight_kg": weight_kg,
        "intake_kcal": round(intake_kcal, 0),
        "maintenance_kcal": round(maintenance_kcal, 0),
        "balance_kcal": round(balance_kcal, 0),
        "surplus": balance_kcal >= 0,
        "estimated_kg": estimate.kilograms,
        "estimated_lean_kg": estimate.lean_kilograms,
        "estimated_fat_kg": estimate.fat_kilograms,
        "kcal_per_kg": round(estimate.kcal_per_kg, 0),
        "first_weight_kg": first_weight,
        "last_weight_kg": last_weight,
        "measured_kg": measured_change,
        "deviation_kg": deviation,
        "deviation_percent": deviation_percent,
        "deviation_note": deviation_note,
        "warnings": warnings,
        "today": local_date(utc_now_iso()),
        "calibration": build_maintenance_calibration(
            conn,
            start_day=start_day,
            end_day=end_day,
        ),
    }
