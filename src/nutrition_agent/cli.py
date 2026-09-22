"""Command line interface for Nutrition Agent."""

import argparse
import logging
import re
import sqlite3

from nutrition_agent.config import settings
from nutrition_agent.db import get_connection, init_db
from nutrition_agent.llm import (
    estimate_food_nutrition,
    estimate_unit_conversion,
    parse_user_message,
)
from nutrition_agent.models import (
    CalculatedWeightLog,
    CalculatedWorkoutLog,
    FoodDefinition,
    FoodNotFoundError,
    WeightValidationError,
    local_date,
    local_timestamp_on,
    parse_date_argument,
    utc_now_iso,
)
from nutrition_agent.nutrition import (
    build_food_from_estimate,
    calculate_food_log,
    convert_to_grams,
    find_matching_food,
    validate_body_weight,
)
from nutrition_agent.reporting import format_daily_target_breakdown, generate_period_report
from nutrition_agent.repository import (
    DAY_LOG_LABELS,
    delete_day_logs,
    delete_food,
    ensure_food_catalog,
    get_daily_food_logs,
    get_food,
    get_recent_food_logs,
    get_recent_weight_logs,
    get_unit_conversion,
    get_user_profile,
    list_foods_by_source,
    list_unit_conversions,
    load_foods,
    save_food_logs,
    save_learned_food,
    save_unit_conversion,
    save_weight_log,
    save_workout_log,
    set_user_setting,
    update_food_nutrition,
)
from nutrition_agent.targets import (
    DayType,
    calculate_daily_targets,
    parse_day_type,
    parse_goal,
)

logger = logging.getLogger(__name__)

_CORRECTION_USAGE = "Použití: jidlo <nazev> [<kcal> <bilkoviny> <sacharidy> <tuky> [<alkohol>]]"


def format_day_counts(counts: dict[str, int]) -> str:
    """Format per-type record counts as a bullet list."""
    return "\n".join(f"- {label}: {counts.get(key, 0)}" for key, label in DAY_LOG_LABELS.items())


def delete_day(conn: sqlite3.Connection, day: str) -> str:
    """Delete every record for a day and describe what was removed."""
    deleted = delete_day_logs(conn, day)
    total = sum(deleted.values())

    if total == 0:
        return f"Pro den {day} nebyly nalezeny žádné záznamy."

    lines = [f"Vymazány záznamy pro den {day}:", format_day_counts(deleted)]
    lines.append(f"Celkem smazáno {total} záznamů.")
    return "\n".join(lines)


def resolve_food(
    conn: sqlite3.Connection,
    food_name: str,
    foods: list[FoodDefinition],
) -> tuple[FoodDefinition, bool]:
    """Resolve a food name against the catalog, falling back to an AI estimate.

    Returns the matched food definition and whether it had to be learned. The
    local catalog always wins; the model is only asked when matching fails.
    """
    try:
        food_def, _ = find_matching_food(food_name, foods)
        return food_def, False
    except FoodNotFoundError:
        logger.info("Potravina '%s' není v katalogu, žádám model o odhad.", food_name)

    estimate = estimate_food_nutrition(food_name)

    # The model names a food the way the catalog usually does ('eidam 30' comes back
    # as 'eidam 30%'), so an estimate that already exists in the catalog is used
    # instead of being stored as a second, differently valued entry.
    try:
        existing, _ = find_matching_food(estimate.name, foods)
    except FoodNotFoundError:
        existing = None
    if existing is not None:
        logger.info("Odhad '%s' odpovídá katalogové potravině '%s'.", estimate.name, existing.name)
        return existing, False

    learned = build_food_from_estimate(estimate)
    save_learned_food(conn, learned)
    return learned, True


def resolve_grams(
    conn: sqlite3.Connection,
    food_def: FoodDefinition,
    amount: float,
    unit: str,
    state: str = "raw",
) -> tuple[float, bool]:
    """Resolve an amount and unit into grams, asking the model as a last resort.

    Order: absolute units, the curated tables, conversions the user or the model
    already stored, and only then a fresh estimate. Returns the grams and whether
    the conversion had to be estimated.

    ``state`` is passed to the estimate because a portion of cooked rice weighs
    roughly 2.5x a portion of dry rice; without it the cooked/raw ratio would
    discount the weight a second time.
    """
    known = convert_to_grams(food_def.name, amount, unit)
    if known is not None:
        return known, False

    learned = get_unit_conversion(conn, food_def.name, unit)
    if learned is not None:
        grams_per_unit, source = learned
        return amount * grams_per_unit, source == "ai"

    logger.info("Neznámý převod '%s' u '%s', žádám model.", unit, food_def.name)
    try:
        estimate = estimate_unit_conversion(food_def.name, unit, state=state)
    except (ValueError, RuntimeError) as exc:
        raise ValueError(
            f"Nevím, kolik gramů je 1 {unit} u potraviny '{food_def.name}' ({exc}). "
            f"Zadej prosím množství v gramech."
        ) from exc

    if estimate.grams_per_unit <= 0:
        raise ValueError(
            f"Nevím, kolik gramů je 1 {unit} u potraviny '{food_def.name}'. "
            f"Zadej prosím množství v gramech."
        )

    save_unit_conversion(conn, food_def.name, unit, estimate.grams_per_unit, source="ai")
    return amount * estimate.grams_per_unit, True


def correct_food_nutrition(conn: sqlite3.Connection, argument: str) -> str:
    """Show or overwrite the nutrition values of a catalog food.

    Usage: 'jidlo <nazev>' to inspect, 'jidlo <nazev> <kcal> <B> <S> <T> [<alkohol>]' to
    overwrite. Alcohol is optional; when omitted the stored value is kept, so a
    correction of a beer does not silently drop its ethanol calories.
    """
    tokens = argument.split()
    if not tokens:
        return _CORRECTION_USAGE

    numbers: list[float] = []
    while tokens and len(numbers) < 5:
        try:
            numbers.insert(0, float(tokens[-1].replace(",", ".")))
        except ValueError:
            break
        tokens.pop()

    name = " ".join(tokens).strip()
    if not name:
        return "Chybí název potraviny. " + _CORRECTION_USAGE

    existing = get_food(conn, name)
    if existing is None:
        return (
            f"Potravina '{name}' není v katalogu. "
            "Nejdřív ji zaznamenej v jídle, pak ji můžeš opravit."
        )

    if not numbers:
        source = "AI odhad" if existing.source == "ai" else "kurátorovaný katalog"
        lines = [
            f"{existing.name} ({source}), hodnoty na 100 g:",
            f"- Energie: {existing.calories:.1f} kcal",
            f"- Bílkoviny: {existing.protein:.2f} g",
            f"- Sacharidy: {existing.carbs:.2f} g",
            f"- Tuky: {existing.fats:.2f} g",
        ]
        if existing.alcohol > 0:
            lines.append(f"- Alkohol: {existing.alcohol:.2f} g ({existing.alcohol * 7:.0f} kcal)")
        lines.append(f"- DIAAS koeficient: {existing.diaas_coefficient:.2f}")
        lines.append(
            f"Oprava: jidlo {existing.name} <kcal> <bilkoviny> <sacharidy> <tuky> [<alkohol>]"
        )
        return "\n".join(lines)

    if len(numbers) not in (4, 5):
        base = f"jidlo {existing.name} <kcal> <bilkoviny> <sacharidy> <tuky> [<alkohol>]"
        return f"Pro opravu zadej čtyři nebo pět hodnot: {base}"

    if min(numbers) < 0:
        return "Hodnoty nesmí být záporné."

    calories, protein, carbs, fats = numbers[:4]
    alcohol = numbers[4] if len(numbers) == 5 else existing.alcohol

    updated = update_food_nutrition(conn, existing.name, calories, protein, carbs, fats, alcohol)
    lines = [
        f"Opraveno: {updated.name} na 100 g:",
        f"- Energie: {updated.calories:.1f} kcal",
        f"- Bílkoviny: {updated.protein:.2f} g",
        f"- Sacharidy: {updated.carbs:.2f} g",
        f"- Tuky: {updated.fats:.2f} g",
    ]
    if updated.alcohol > 0:
        lines.append(f"- Alkohol: {updated.alcohol:.2f} g ({updated.alcohol * 7:.0f} kcal)")
    lines.append("Už zaznamenané dny se nepřepočítávají.")
    return "\n".join(lines)


def list_catalog_entries(conn: sqlite3.Connection, argument: str) -> str:
    """List catalog foods, either all of them or only the AI-estimated ones."""
    selector = argument.strip().lower()
    if selector in ("", "vse", "vše", "all"):
        foods = load_foods(conn)
        title = "Katalog potravin"
    elif selector in ("ai", "odhad", "odhady"):
        foods = list_foods_by_source(conn, "ai")
        title = "Potraviny naučené odhadem AI"
    elif selector in ("curated", "katalog", "rucni", "ruční"):
        foods = list_foods_by_source(conn, "curated")
        title = "Kurátorované potraviny"
    else:
        return "Použití: jidla [ai | curated | vse]"

    if not foods:
        return f"{title}: žádné záznamy."

    lines = [f"{title} ({len(foods)}):"]
    for food in foods:
        lines.append(
            f"- {food.name}: {food.calories:.0f} kcal, {food.protein:.1f} g bílkovin, "
            f"{food.carbs:.1f} g sacharidů, {food.fats:.1f} g tuků na 100 g"
        )
    if selector in ("ai", "odhad", "odhady"):
        lines.append("Smazání: jidla smaz <nazev>")
        lines.append("Oprava hodnot: jidlo <nazev> <kcal> <bilkoviny> <sacharidy> <tuky>")
    return "\n".join(lines)


def delete_catalog_food(conn: sqlite3.Connection, argument: str) -> str:
    """Delete a food from the catalog by name."""
    name = argument.strip()
    if not name:
        return "Použití: jidla smaz <nazev>"

    try:
        removed = delete_food(conn, name)
    except LookupError:
        return f"Potravina '{name}' není v katalogu."

    note = (
        " Už zaznamenané dny zůstávají beze změny."
        if removed.source == "ai"
        else " Pozor: šlo o kurátorovanou potravinu z katalogu."
    )
    return f"Smazáno z katalogu: {removed.name}.{note}"


def show_unit_conversions(conn: sqlite3.Connection, argument: str) -> str:
    """Show or correct learned grams-per-unit conversions.

    Usage: 'prevod' to list, 'prevod <nazev> <jednotka> <gramy>' to overwrite.
    """
    tokens = argument.split()
    if not tokens:
        conversions = list_unit_conversions(conn)
        if not conversions:
            return "Žádné naučené převody jednotek."
        lines = [f"Naučené převody jednotek ({len(conversions)}):"]
        for item in conversions:
            source = "uživatel" if item["source"] == "user" else "AI odhad"
            lines.append(
                f"- {item['food_key']} | 1 {item['unit']} = "
                f"{item['grams_per_unit']:.1f} g ({source})"
            )
        lines.append("Oprava: prevod <nazev> <jednotka> <gramy>")
        return "\n".join(lines)

    if len(tokens) < 3:
        return "Použití: prevod [<nazev> <jednotka> <gramy>]"

    try:
        grams = float(tokens[-1].replace(",", "."))
    except ValueError:
        return f"Hodnota '{tokens[-1]}' není číslo."

    unit = tokens[-2].lower()
    name = " ".join(tokens[:-2]).strip()
    if not name:
        return "Chybí název potraviny. Použití: prevod <nazev> <jednotka> <gramy>"

    try:
        save_unit_conversion(conn, name, unit, grams, source="user")
    except ValueError as exc:
        return f"Chyba: {exc}"

    return f"Převod uložen: {name} | 1 {unit} = {grams:.1f} g (uživatel)."


def format_history(conn: sqlite3.Connection, limit: int = 10) -> str:
    """Format recent food and weight logs."""
    food_logs = get_recent_food_logs(conn, limit=limit)
    weight_logs = get_recent_weight_logs(conn, limit=limit)

    if not food_logs and not weight_logs:
        return "Historie je prázdná. Nebyly nalezeny žádné záznamy."

    lines = ["Historie záznamů:"]

    if food_logs:
        lines.append(f"\nPoslední jídla (posledních {len(food_logs)}):")
        for item in food_logs:
            time_part = item.logged_at[:16].replace("T", " ")
            ai_flag = " [AI odhad]" if item.nutrition_source == "ai" else ""
            conv_flag = " [odhad převodu]" if item.conversion_estimated else ""
            lines.append(
                f"- {time_part} | {item.food_name_matched} ({item.amount_g:.0f} g, {item.state}) | "
                f"{item.calories:.1f} kcal | Bílkoviny: {item.protein_usable:.1f} g DIAAS"
                f"{ai_flag}{conv_flag}"
            )

    if weight_logs:
        lines.append(f"\nPoslední vážení (posledních {len(weight_logs)}):")
        for item in weight_logs:
            time_part = item.logged_at[:16].replace("T", " ")
            lines.append(f"- {time_part} | {item.weight_kg:.1f} kg")

    return "\n".join(lines)


def handle_user_message(
    user_input: str,
    foods: list[FoodDefinition],
    conn: sqlite3.Connection,
    parse_fn=parse_user_message,
    target_day: str | None = None,
) -> str:
    """Process a user message through parser, nutrition calculation and persistence.

    ``target_day`` backdates the resulting records to that local day (YYYY-MM-DD),
    which the dashboard uses to fill in a past day retroactively.
    """
    clean_input = user_input.strip()
    if not clean_input:
        return "Zadán prázdný vstup."

    logged_at = local_timestamp_on(target_day) if target_day else None

    command_lower = clean_input.lower()

    # 1. State and Goal management commands
    parts = clean_input.split(maxsplit=1)
    first_token = parts[0].lower()
    if first_token in ("cil", "cíl", "stav", "goal") and len(parts) > 1:
        try:
            goal = parse_goal(parts[1])
        except ValueError as exc:
            return str(exc)
        set_user_setting(conn, "goal", goal.value)
        _, day_type, weight = get_user_profile(conn)
        targets = calculate_daily_targets(weight, goal, day_type)
        return (
            f"Aktivní cíl byl změněn na: {goal.value.upper()}.\n"
            f"Přepočtené denní cíle ({weight:.1f} kg, {day_type.value}):\n"
            f"Energie: {targets.calories:.0f} kcal | "
            f"Bílkoviny: {targets.protein_usable:.1f} g DIAAS | "
            f"Sacharidy: {targets.carbs:.1f} g | "
            f"Tuky: {targets.fats:.1f} g"
        )

    if first_token in ("den", "day") and len(parts) > 1:
        try:
            day_type = parse_day_type(parts[1])
        except ValueError as exc:
            return str(exc)
        set_user_setting(conn, "day_type", day_type.value)
        goal, _, weight = get_user_profile(conn)
        targets = calculate_daily_targets(weight, goal, day_type)
        return (
            f"Typ dne byl nastaven na: {day_type.value.upper()}.\n"
            f"Aktuální denní cíle ({goal.value}, {weight:.1f} kg):\n"
            f"Energie: {targets.calories:.0f} kcal | "
            f"Bílkoviny: {targets.protein_usable:.1f} g DIAAS | "
            f"Sacharidy: {targets.carbs:.1f} g | "
            f"Tuky: {targets.fats:.1f} g"
        )

    if first_token in ("trenink", "trénink", "workout"):
        rest = parts[1].strip() if len(parts) > 1 else "Silový trénink"
        dur_match = re.search(r"(\d+)\s*(?:min|m)?", rest)
        dur = int(dur_match.group(1)) if dur_match else None
        clean_desc = re.sub(r"\d+\s*(?:min|m)?", "", rest).strip() or "Silový trénink"

        workout_log = CalculatedWorkoutLog(
            raw_input=clean_input,
            workout_type=clean_desc,
            duration_minutes=dur,
        )
        if logged_at:
            workout_log = workout_log.model_copy(update={"logged_at": logged_at})
        save_workout_log(conn, workout_log)
        set_user_setting(conn, "day_type", DayType.TRAINING.value)
        dur_info = f" ({dur} min)" if dur else ""
        day_info = f" na den {target_day}" if target_day else ""
        return (
            f"Zaznamenán trénink: {clean_desc}{dur_info}{day_info}.\n"
            "Typ dne byl automaticky nastaven na TRAINING."
        )

    if first_token in ("vymaz", "vymaž", "smaz", "smaž", "delete"):
        raw_day = parts[1] if len(parts) > 1 else None
        try:
            day = parse_date_argument(raw_day)
        except ValueError as exc:
            return str(exc)
        return delete_day(conn, day)

    if first_token in ("jidlo", "jídlo", "potravina", "food"):
        if len(parts) < 2:
            return "Použití: jidlo <nazev> [<kcal> <bilkoviny> <sacharidy> <tuky>]"
        return correct_food_nutrition(conn, parts[1])

    if first_token in ("jidla", "jídla", "potraviny", "foods"):
        if len(parts) > 1 and parts[1].lower().startswith(("smaz", "smaž", "vymaz", "vymaž")):
            inner = parts[1].split(maxsplit=1)
            return delete_catalog_food(conn, inner[1] if len(inner) > 1 else "")
        return list_catalog_entries(conn, parts[1] if len(parts) > 1 else "")

    if first_token in ("prevod", "převod", "jednotka", "jednotky"):
        return show_unit_conversions(conn, parts[1] if len(parts) > 1 else "")

    # 2. Timeframe reporting commands
    if command_lower in ("dnes", "prehled", "přehled", "dnesek", "summary", "today"):
        return generate_period_report(conn, "denni")
    if command_lower in ("tyden", "týden", "tydenni", "týdenní", "week"):
        return generate_period_report(conn, "tydenni")
    if command_lower in ("mesic", "měsíc", "mesicni", "měsíční", "month"):
        return generate_period_report(conn, "mesicni")
    if command_lower in ("pulrok", "půlrok", "pulrocni", "půlroční", "half_year"):
        return generate_period_report(conn, "pulrocni")
    if command_lower in ("rok", "rocni", "roční", "year"):
        return generate_period_report(conn, "rocni")
    if command_lower in ("vse", "vše", "kompletni", "kompletní", "all", "total"):
        return generate_period_report(conn, "kompletni")
    if command_lower in ("historie", "history"):
        return format_history(conn)

    # 3. LLM Parsing
    try:
        user_log = parse_fn(clean_input)
    except Exception as exc:
        return f"Chyba při komunikaci s LLM parserem: {exc}"

    if user_log.log_type == "INVALID":
        reason = user_log.error_reason or "Neplatný formát vstupu."
        return f"Chyba vstupu: {reason}"

    if user_log.log_type == "WEIGHT":
        if user_log.weight_kg is None:
            return "Chyba vstupu: Chybí hodnota tělesné hmotnosti."
        try:
            valid_weight = validate_body_weight(user_log.weight_kg)
        except WeightValidationError as exc:
            return f"Chyba validace: {exc}"

        weight_log = CalculatedWeightLog(
            raw_input=clean_input,
            weight_kg=valid_weight,
        )
        if logged_at:
            weight_log = weight_log.model_copy(update={"logged_at": logged_at})
        save_weight_log(conn, weight_log)

        goal, day_type, _ = get_user_profile(conn)
        new_targets = calculate_daily_targets(valid_weight, goal, day_type)
        day_info = f" na den {target_day}" if target_day else ""
        return (
            f"Zaznamenána tělesná hmotnost{day_info}: {valid_weight:.1f} kg\n"
            f"Cílové hodnoty byly automaticky přepočteny pro novou hmotnost ({goal.value}, "
            f"{day_type.value}): {new_targets.calories:.0f} kcal | "
            f"{new_targets.protein_usable:.1f} g DIAAS bílkovin | "
            f"{new_targets.carbs:.1f} g sacharidů | {new_targets.fats:.1f} g tuků."
        )

    if user_log.log_type == "FOOD":
        if not user_log.items:
            return "Chyba: Záznam jídla neobsahuje žádné položky."

        calculated_items = []
        learned_names: list[str] = []
        estimated_conversions: list[tuple[str, str]] = []
        for item in user_log.items:
            try:
                food_def, was_learned = resolve_food(conn, item.name, foods)
                amount_g, conversion_estimated = resolve_grams(
                    conn, food_def, item.amount, item.unit, state=item.state
                )
            except FoodNotFoundError as exc:
                return f"Chyba: {exc}"
            except (ValueError, RuntimeError) as exc:
                return f"Chyba: {exc}"

            if was_learned:
                # Keep the in-memory catalog in sync for the remaining items.
                foods.append(food_def)
                learned_names.append(food_def.name)

            if conversion_estimated:
                estimated_conversions.append((food_def.name, item.unit))

            calculated = calculate_food_log(
                item,
                food_def,
                clean_input,
                amount_g,
                conversion_estimated=conversion_estimated,
            )
            if logged_at:
                calculated = calculated.model_copy(update={"logged_at": logged_at})
            calculated_items.append(calculated)

        save_food_logs(conn, calculated_items)

        header = "Zaznamenáno jídlo:"
        if target_day:
            header = f"Zaznamenáno jídlo na den {target_day}:"
        lines = [header]
        for item in calculated_items:
            est_flag = " (odhad množství)" if item.is_estimated else ""
            ai_flag = " [AI odhad maker]" if item.nutrition_source == "ai" else ""
            conv_flag = " [odhad převodu]" if item.conversion_estimated else ""
            state_flag = f" ({item.state})" if item.state == "cooked" else ""
            alcohol_flag = f" | Alkohol: {item.alcohol:.1f} g" if item.alcohol > 0 else ""
            lines.append(
                f"- {item.food_name_matched} ({item.amount_g:.0f} g{state_flag}{est_flag}): "
                f"{item.calories:.1f} kcal | Bílkoviny: {item.protein_usable:.1f} g DIAAS "
                f"(celkem {item.protein_raw:.1f} g) | Sacharidy: {item.carbs:.1f} g | "
                f"Tuky: {item.fats:.1f} g{alcohol_flag}{ai_flag}{conv_flag}"
            )

        if learned_names:
            lines.append("---")
            lines.append(
                "Tyto potraviny nebyly v katalogu, takže je model odhadl a uložil "
                "jako odhad (příště se použijí stejné hodnoty):"
            )
            for name in learned_names:
                stored = get_food(conn, name)
                if stored is not None:
                    lines.append(
                        f"- {stored.name}: {stored.calories:.0f} kcal, "
                        f"{stored.protein:.1f} g bílkovin, {stored.carbs:.1f} g sacharidů, "
                        f"{stored.fats:.1f} g tuků na 100 g"
                    )
            lines.append(
                "Hodnoty jde opravit příkazem: "
                f"jidlo {learned_names[0]} <kcal> <bilkoviny> <sacharidy> <tuky>"
            )

        if estimated_conversions:
            lines.append("---")
            lines.append(
                "U těchto položek jsem neznal hmotnost jednotky, takže ji model odhadl "
                "a uložil (příště se použije stejná):"
            )
            for name, unit in estimated_conversions:
                learned = get_unit_conversion(conn, name, unit)
                grams = f"{learned[0]:.0f} g" if learned else "?"
                lines.append(f"- {name}: 1 {unit} = {grams}")
            first_name, first_unit = estimated_conversions[0]
            lines.append(f"Oprava: prevod {first_name} {first_unit} <gramy>")

        report_day = target_day or local_date(utc_now_iso())
        daily_foods = get_daily_food_logs(conn, report_day)
        day_cal = sum(x.calories for x in daily_foods)
        day_prot = sum(x.protein_usable for x in daily_foods)
        day_carbs = sum(x.carbs for x in daily_foods)
        day_fats = sum(x.fats for x in daily_foods)

        goal, day_type, current_weight = get_user_profile(conn)
        targets = calculate_daily_targets(current_weight, goal, day_type)

        lines.append("---")
        if target_day:
            lines.append(f"Součet za den {report_day}:")
        lines.append(format_daily_target_breakdown(day_cal, day_prot, day_carbs, day_fats, targets))
        return "\n".join(lines)

    return "Nerozpoznaný typ záznamu."


def run_interactive_loop(foods: list[FoodDefinition], conn: sqlite3.Connection) -> None:
    """Run interactive CLI chat loop."""
    goal, day_type, weight = get_user_profile(conn)
    targets = calculate_daily_targets(weight, goal, day_type)

    print("Nutrition Agent spuštěn.")
    print(
        f"Aktivní profil: {goal.value.upper()} | {day_type.value.upper()} | "
        f"{weight:.1f} kg ({targets.calories:.0f} kcal)"
    )
    print("Příkazy: 'dnes', 'tyden', 'mesic', 'pulrok', 'rok', 'vse', 'historie'")
    print("Správa cílů: 'cil [bulk|maintenance|cut]', 'den [trenink|odpocinek]'")
    print("Zadejte jídlo, váhu nebo příkaz (konec pro ukončení):")

    while True:
        try:
            user_input = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nUkončuji.")
            break

        if not user_input:
            continue

        if user_input.lower() in ("konec", "exit", "quit", "q"):
            print("Nashledanou.")
            break

        response = handle_user_message(user_input, foods, conn)
        print(response)


def run_all_services(include_bot: bool = True, open_browser: bool = False) -> None:
    """Run the web dashboard, optionally with the Discord bot, in one event loop.

    The web dashboard keeps running even when the Discord bot cannot start, so a
    missing or invalid bot token never takes the dashboard down with it.
    """
    import asyncio
    import webbrowser

    import uvicorn

    web_url = f"http://{settings.web_host}:{settings.web_port}"
    print(f"Web dashboard: {web_url}")

    has_discord_token = bool(settings.discord_bot_token and settings.discord_bot_token.strip())
    if not include_bot:
        print("Discord bot: vypnut (rezim pouze web)")
    elif has_discord_token:
        print("Discord bot: spoustim")
    else:
        print("Discord bot: preskocen (DISCORD_BOT_TOKEN neni nastaven v .env)")
    print("Pro ukonceni stisknete Ctrl+C.")
    print()

    start_bot = include_bot and has_discord_token

    async def _serve_bot() -> None:
        from nutrition_agent.bot import run_bot_async

        try:
            await run_bot_async()
        except (Exception, SystemExit) as exc:
            print()
            print(f"Discord bot skoncil: {exc}")
            print("Web dashboard bezi dal. Pro ukonceni stisknete Ctrl+C.")

    async def _open_browser_when_ready(server: uvicorn.Server) -> None:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.1)
        webbrowser.open(web_url)

    async def _serve_all() -> None:
        from nutrition_agent.web import create_app

        config = uvicorn.Config(
            create_app(),
            host=settings.web_host,
            port=settings.web_port,
            log_level="info",
        )
        server = uvicorn.Server(config)

        tasks = [asyncio.create_task(server.serve())]
        if start_bot:
            tasks.append(asyncio.create_task(_serve_bot()))
        if open_browser:
            tasks.append(asyncio.create_task(_open_browser_when_ready(server)))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                print(f"Sluzba skoncila s chybou: {result}")

    try:
        asyncio.run(_serve_all())
    except KeyboardInterrupt:
        print("\nUkonceno.")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Nutrition Agent - minimalistický nutriční asistent"
    )
    parser.add_argument("message", nargs="*", help="Jednorázová zpráva k zaznamenání")
    parser.add_argument(
        "--today",
        "--dnes",
        "-t",
        action="store_true",
        help="Zobrazit dnešní přehled stravy a hmotnosti",
    )
    parser.add_argument(
        "--week",
        "--tyden",
        action="store_true",
        help="Zobrazit týdenní souhrn (posledních 7 dní)",
    )
    parser.add_argument(
        "--month",
        "--mesic",
        action="store_true",
        help="Zobrazit měsíční souhrn (posledních 30 dní)",
    )
    parser.add_argument(
        "--half-year",
        "--pulrok",
        action="store_true",
        help="Zobrazit půlroční souhrn (posledních 180 dní)",
    )
    parser.add_argument(
        "--year",
        "--rok",
        action="store_true",
        help="Zobrazit roční souhrn (posledních 365 dní)",
    )
    parser.add_argument(
        "--all",
        "--vse",
        "--kompletni",
        action="store_true",
        help="Zobrazit kompletní souhrn ze všech dat",
    )
    parser.add_argument(
        "--goal",
        "--cil",
        type=str,
        help="Nastavit aktivní nutriční cíl (lean_bulk, maintenance, cut)",
    )
    parser.add_argument(
        "--day-type",
        "--den",
        type=str,
        help="Nastavit typ dne (training, rest)",
    )
    parser.add_argument(
        "--history",
        "-H",
        nargs="?",
        const=10,
        type=int,
        default=None,
        help="Zobrazit posledních N záznamů (výchozí 10)",
    )
    parser.add_argument(
        "--delete-day",
        "--vymaz-den",
        nargs="?",
        const="",
        default=None,
        metavar="DATUM",
        help="Vymazat všechny záznamy za den (RRRR-MM-DD, 'dnes' nebo 'vcera')",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Spustit webový dashboard server",
    )
    parser.add_argument(
        "--bot",
        action="store_true",
        help="Spustit Discord bota",
    )
    parser.add_argument(
        "--run-all",
        action="store_true",
        help="Spustit souběžně webový dashboard server i Discord bota",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Po startu webového dashboardu otevřít prohlížeč",
    )
    args = parser.parse_args()

    # Standalone service runners
    if args.web or (args.message and args.message[0] == "web"):
        run_all_services(include_bot=False, open_browser=args.open_browser)
        return

    if args.bot or (args.message and args.message[0] == "bot"):
        from nutrition_agent.bot import run_bot

        run_bot()
        return

    if args.run_all or (args.message and args.message[0] in ("run", "all")):
        run_all_services(include_bot=True, open_browser=args.open_browser)
        return

    init_db(settings.database_path)
    conn = get_connection(settings.database_path)
    ensure_food_catalog(conn, settings.foods_data_path)
    foods = load_foods(conn)

    try:
        if args.goal:
            goal = parse_goal(args.goal)
            set_user_setting(conn, "goal", goal.value)
            print(f"Aktivní cíl nastaven na: {goal.value}")

        if args.day_type:
            day_type = parse_day_type(args.day_type)
            set_user_setting(conn, "day_type", day_type.value)
            print(f"Typ dne nastaven na: {day_type.value}")

        if args.delete_day is not None:
            try:
                day = parse_date_argument(args.delete_day)
            except ValueError as exc:
                print(str(exc))
            else:
                print(delete_day(conn, day))
        elif args.today:
            print(generate_period_report(conn, "denni"))
        elif args.week:
            print(generate_period_report(conn, "tydenni"))
        elif args.month:
            print(generate_period_report(conn, "mesicni"))
        elif args.half_year:
            print(generate_period_report(conn, "pulrocni"))
        elif args.year:
            print(generate_period_report(conn, "rocni"))
        elif args.all:
            print(generate_period_report(conn, "kompletni"))
        elif args.history is not None:
            print(format_history(conn, limit=args.history))
        elif args.message:
            full_message = " ".join(args.message)
            response = handle_user_message(full_message, foods, conn)
            print(response)
        elif not args.goal and not args.day_type:
            run_interactive_loop(foods, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
