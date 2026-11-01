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

# Below this share of logged days the balance is a guess, not a measurement.
MIN_LOGGED_SHARE: float = 0.70

# Minimum distinct-day weigh-ins required to calibrate the maintenance baseline.
MIN_CALIBRATION_WEIGH_INS: int = 7


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


def energy_for_mass_change(delta_kg: float, days: int = 30) -> float:
    """Return the tissue energy (kcal) represented by a measured scale change."""
    if delta_kg < 0:
        return delta_kg * LOSS_KCAL_PER_KG

    effective_days = max(days, 1)
    max_lean_kg = MAX_LEAN_GAIN_KG_PER_30_DAYS * (effective_days / 30.0)
    max_mixed_kg = 2.0 * max_lean_kg
    base_kcal_per_kg = energy_per_kg_of_gain()

    if delta_kg <= max_mixed_kg:
        return delta_kg * base_kcal_per_kg

    cap_kcal = max_mixed_kg * base_kcal_per_kg
    return cap_kcal + (delta_kg - max_mixed_kg) * FAT_KCAL_PER_KG


def _regression_weight_change(
    daily_weights: list[tuple[str, float]],
) -> tuple[float, float, float]:
    """Fit a least-squares line over the weigh-ins to smooth out daily water noise.

    Returns (trend_start_kg, trend_end_kg, delta_kg).
    """
    start_dt = datetime.strptime(daily_weights[0][0], "%Y-%m-%d")
    xs = [float((datetime.strptime(day, "%Y-%m-%d") - start_dt).days) for day, _ in daily_weights]
    ys = [weight for _, weight in daily_weights]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom <= 0:
        start_w = round(ys[0], 2)
        end_w = round(ys[-1], 2)
        return start_w, end_w, round(end_w - start_w, 2)

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom
    intercept = mean_y - slope * mean_x
    trend_start = intercept
    trend_end = intercept + slope * xs[-1]
    return round(trend_start, 2), round(trend_end, 2), round(trend_end - trend_start, 2)


def build_maintenance_calibration(
    conn,
    min_weigh_ins: int = MIN_CALIBRATION_WEIGH_INS,
) -> dict[str, object]:
    """Compute a calibrated maintenance baseline from the last `min_weigh_ins` weigh-ins.

    Uses the span from the first to the last of those weigh-ins, smooths weight change
    across all weigh-ins via linear regression, and compares the observed tissue energy
    against the logged calorie surplus/deficit over that same span.
    """
    all_logs = get_weight_logs_in_range(conn)
    by_day: dict[str, float] = {}
    for log in all_logs:
        by_day[local_date(log.logged_at)] = log.weight_kg
    daily_weights = sorted(by_day.items(), key=lambda item: item[0])

    calorie_offset = get_calorie_offset(conn)
    if len(daily_weights) < min_weigh_ins:
        return {
            "ready": False,
            "weigh_ins_count": len(daily_weights),
            "required_weigh_ins": min_weigh_ins,
            "start_date": daily_weights[0][0] if daily_weights else None,
            "end_date": daily_weights[-1][0] if daily_weights else None,
            "span_days": 0,
            "logged_days": 0,
            "trend_start_kg": None,
            "trend_end_kg": None,
            "delta_kg": None,
            "current_maintenance_kcal": None,
            "recommended_maintenance_kcal": None,
            "recommended_delta_kcal": None,
            "calorie_offset_kcal": calorie_offset,
        }

    window_weights = daily_weights[-min_weigh_ins:]
    start_day = window_weights[0][0]
    end_day = window_weights[-1][0]

    summaries = get_day_summaries(conn, start_day, end_day)
    logged_days = [day for day in summaries if day["has_food"]]
    if not logged_days:
        return {
            "ready": False,
            "weigh_ins_count": len(window_weights),
            "required_weigh_ins": min_weigh_ins,
            "start_date": start_day,
            "end_date": end_day,
            "span_days": len(summaries),
            "logged_days": 0,
            "trend_start_kg": None,
            "trend_end_kg": None,
            "delta_kg": None,
            "current_maintenance_kcal": None,
            "recommended_maintenance_kcal": None,
            "recommended_delta_kcal": None,
            "calorie_offset_kcal": calorie_offset,
        }

    _, _, weight_kg = get_user_profile(conn)
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

    trend_start_kg, trend_end_kg, delta_kg = _regression_weight_change(window_weights)
    span_days = max(len(summaries), 1)
    tissue_kcal = energy_for_mass_change(delta_kg, days=span_days)

    avg_maintenance_kcal = maintenance_kcal / len(logged_days)
    daily_logged_balance = (intake_kcal - maintenance_kcal) / len(logged_days)
    daily_tissue_kcal = tissue_kcal / span_days
    recommended_delta_kcal = round(daily_logged_balance - daily_tissue_kcal, 0)
    recommended_maintenance_kcal = round(avg_maintenance_kcal + recommended_delta_kcal, 0)

    return {
        "ready": True,
        "weigh_ins_count": len(window_weights),
        "required_weigh_ins": min_weigh_ins,
        "start_date": start_day,
        "end_date": end_day,
        "span_days": span_days,
        "logged_days": len(logged_days),
        "trend_start_kg": trend_start_kg,
        "trend_end_kg": trend_end_kg,
        "delta_kg": delta_kg,
        "current_maintenance_kcal": round(avg_maintenance_kcal, 0),
        "recommended_maintenance_kcal": recommended_maintenance_kcal,
        "recommended_delta_kcal": recommended_delta_kcal,
        "calorie_offset_kcal": calorie_offset,
    }


def apply_maintenance_calibration(
    conn,
    min_weigh_ins: int = MIN_CALIBRATION_WEIGH_INS,
) -> dict[str, object]:
    """Persist the calibrated maintenance offset and shift all goal targets accordingly."""
    calibration = build_maintenance_calibration(conn, min_weigh_ins=min_weigh_ins)
    if not calibration["ready"]:
        raise ValueError(
            f"Pro kalibraci maintenance je potřeba alespoň {min_weigh_ins} vážení "
            f"(aktuálně {calibration['weigh_ins_count']}) a záznamy jídla v daném období."
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
        "calibration": build_maintenance_calibration(conn, min_weigh_ins=min_weigh_ins),
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
        "calibration": build_maintenance_calibration(conn),
    }
