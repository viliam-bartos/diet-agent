"""Energy balance against maintenance and the body mass it explains.

The comparison needs a maintenance baseline and an energy cost for the tissue that is
gained or lost. Both are estimates, so the numbers are constants with the reasoning
next to them rather than magic values inside a function.

Sources behind the constants:
- adipose tissue carries about 7700 kcal per kg (the classic Wishnofsky rule),
- lean tissue is mostly water, so gaining a kilo of it costs roughly 1800 kcal
  (Forbes, deliberate overfeeding: energy cost and composition of the weight gain,
  Br J Nutr 1986, https://pubmed.ncbi.nlm.nih.gov/3479191/),
- in a lean person who lifts, roughly 70 % of gained mass is lean and 30 % fat; that
  share falls as body fat rises, which is why it is a documented assumption here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from nutrition_agent.models import local_date, utc_now_iso
from nutrition_agent.repository import (
    get_day_summaries,
    get_user_profile,
    get_weight_logs_between_days,
)
from nutrition_agent.targets import DayType, DietGoal, calculate_daily_targets

FAT_KCAL_PER_KG: float = 7700.0
LEAN_KCAL_PER_KG: float = 1800.0
LEAN_SHARE_OF_GAIN: float = 0.70
# Losses come mostly from fat, so a deficit is converted with the adipose energy density.
LOSS_KCAL_PER_KG: float = FAT_KCAL_PER_KG

# Below this share of logged days the balance is a guess, not a measurement.
MIN_LOGGED_SHARE: float = 0.70


@dataclass(frozen=True)
class MassEstimate:
    """What a given energy surplus or deficit should mean for body mass."""

    kilograms: float
    lean_kilograms: float
    fat_kilograms: float
    kcal_per_kg: float
    gain: bool


def energy_per_kg_of_gain() -> float:
    """Return the energy needed for one kilo of mixed tissue in a lean bulk."""
    return round(
        LEAN_SHARE_OF_GAIN * LEAN_KCAL_PER_KG + (1.0 - LEAN_SHARE_OF_GAIN) * FAT_KCAL_PER_KG, 1
    )


def estimate_mass_change(energy_balance_kcal: float) -> MassEstimate:
    """Convert an energy surplus (or deficit) into the mass it explains.

    A surplus is split between lean and fat mass by the documented share; a deficit is
    treated as mostly fat, because losing muscle needs a much larger deficit.
    """
    gain = energy_balance_kcal >= 0
    kcal_per_kg = energy_per_kg_of_gain() if gain else LOSS_KCAL_PER_KG
    kilograms = round(energy_balance_kcal / kcal_per_kg, 2)

    if gain:
        lean = round(kilograms * LEAN_SHARE_OF_GAIN, 2)
        fat = round(kilograms - lean, 2)
    else:
        lean = 0.0
        fat = kilograms

    return MassEstimate(
        kilograms=kilograms,
        lean_kilograms=lean,
        fat_kilograms=fat,
        kcal_per_kg=kcal_per_kg,
        gain=gain,
    )


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

    intake_kcal = 0.0
    maintenance_kcal = 0.0
    for day in logged_days:
        intake_kcal += day["calories"]
        maintenance = calculate_daily_targets(
            weight_kg, DietGoal.MAINTENANCE, DayType(day["day_type"])
        )
        maintenance_kcal += maintenance.calories

    balance_kcal = intake_kcal - maintenance_kcal
    estimate = estimate_mass_change(balance_kcal)

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
    }
