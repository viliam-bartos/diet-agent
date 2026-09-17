"""Command line interface for Nutrition Agent."""

import argparse
import sqlite3
from datetime import datetime

from nutrition_agent.config import settings
from nutrition_agent.db import get_connection, init_db
from nutrition_agent.llm import parse_user_message
from nutrition_agent.models import (
    CalculatedWeightLog,
    FoodDefinition,
    FoodNotFoundError,
    WeightValidationError,
)
from nutrition_agent.nutrition import (
    calculate_food_log,
    find_matching_food,
    validate_body_weight,
)
from nutrition_agent.repository import (
    get_daily_food_logs,
    get_latest_weight_log,
    get_recent_food_logs,
    get_recent_weight_logs,
    load_foods_from_json,
    save_food_logs,
    save_weight_log,
)


def format_daily_summary(conn: sqlite3.Connection, target_date: str | None = None) -> str:
    """Format daily nutrition totals and meal breakdown for a given date."""
    date_str = target_date or datetime.now().strftime("%Y-%m-%d")
    food_logs = get_daily_food_logs(conn, date_str)
    latest_weight = get_latest_weight_log(conn, date_str)

    if not food_logs and latest_weight is None:
        return f"Pro datum {date_str} nejsou evidovány žádné záznamy."

    lines = [f"Denní přehled ({date_str}):"]

    if food_logs:
        lines.append(f"Zaznamenaná jídla ({len(food_logs)}):")
        total_kcal = 0.0
        total_prot_usable = 0.0
        total_prot_raw = 0.0
        total_carbs = 0.0
        total_fats = 0.0

        for item in food_logs:
            time_part = item.logged_at[11:16] if len(item.logged_at) >= 16 else ""
            prefix = f"- [{time_part}] " if time_part else "- "
            est_flag = " (odhad)" if item.is_estimated else ""
            state_flag = f" ({item.state})" if item.state == "cooked" else ""
            lines.append(
                f"{prefix}{item.food_name_matched} ({item.amount_g:.0f} g{state_flag}{est_flag}): "
                f"{item.calories:.1f} kcal | Bílkoviny: {item.protein_usable:.1f} g DIAAS "
                f"(celkem {item.protein_raw:.1f} g) | Sacharidy: {item.carbs:.1f} g | "
                f"Tuky: {item.fats:.1f} g"
            )
            total_kcal += item.calories
            total_prot_usable += item.protein_usable
            total_prot_raw += item.protein_raw
            total_carbs += item.carbs
            total_fats += item.fats

        lines.append("---")
        lines.append("Součet za den:")
        lines.append(f"Energie: {total_kcal:.1f} kcal")
        lines.append(f"Bílkoviny (využitelné DIAAS): {total_prot_usable:.1f} g")
        lines.append(f"Bílkoviny (celkem): {total_prot_raw:.1f} g")
        lines.append(f"Sacharidy: {total_carbs:.1f} g")
        lines.append(f"Tuky: {total_fats:.1f} g")

    if latest_weight:
        time_part = latest_weight.logged_at[11:16] if len(latest_weight.logged_at) >= 16 else ""
        time_info = f" (v {time_part})" if time_part else ""
        lines.append(f"Tělesná hmotnost: {latest_weight.weight_kg:.1f} kg{time_info}")

    return "\n".join(lines)


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
            lines.append(
                f"- {time_part} | {item.food_name_matched} ({item.amount_g:.0f} g, {item.state}) | "
                f"{item.calories:.1f} kcal | Bílkoviny: {item.protein_usable:.1f} g DIAAS"
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
) -> str:
    """Process user message through parser, nutrition calculation, and persistence."""
    clean_input = user_input.strip()
    if not clean_input:
        return "Zadán prázdný vstup."

    # Intercept built-in commands
    command_lower = clean_input.lower()
    if command_lower in ("dnes", "prehled", "dnesek", "summary", "stats", "today"):
        return format_daily_summary(conn)
    if command_lower in ("historie", "history"):
        return format_history(conn)

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
        save_weight_log(conn, weight_log)
        return f"Zaznamenána tělesná hmotnost: {valid_weight:.1f} kg"

    if user_log.log_type == "FOOD":
        if not user_log.items:
            return "Chyba: Záznam jídla neobsahuje žádné položky."

        calculated_items = []
        for item in user_log.items:
            try:
                food_def, _ = find_matching_food(item.name, foods)
            except FoodNotFoundError as exc:
                return f"Chyba: {exc}"

            calculated = calculate_food_log(item, food_def, clean_input)
            calculated_items.append(calculated)

        save_food_logs(conn, calculated_items)

        lines = ["Zaznamenáno jídlo:"]
        total_kcal = 0.0
        total_prot_raw = 0.0
        total_prot_usable = 0.0
        total_carbs = 0.0
        total_fats = 0.0

        for item in calculated_items:
            est_flag = " (odhad)" if item.is_estimated else ""
            state_flag = f" ({item.state})" if item.state == "cooked" else ""
            lines.append(
                f"- {item.food_name_matched} ({item.amount_g:.0f} g{state_flag}{est_flag}): "
                f"{item.calories:.1f} kcal | Bílkoviny: {item.protein_usable:.1f} g DIAAS "
                f"(celkem {item.protein_raw:.1f} g) | Sacharidy: {item.carbs:.1f} g | "
                f"Tuky: {item.fats:.1f} g"
            )
            total_kcal += item.calories
            total_prot_raw += item.protein_raw
            total_prot_usable += item.protein_usable
            total_carbs += item.carbs
            total_fats += item.fats

        lines.append("---")
        lines.append(
            f"Celkem: {total_kcal:.1f} kcal | "
            f"Bílkoviny: {total_prot_usable:.1f} g DIAAS (celkem {total_prot_raw:.1f} g) | "
            f"Sacharidy: {total_carbs:.1f} g | "
            f"Tuky: {total_fats:.1f} g"
        )
        return "\n".join(lines)

    return "Nerozpoznaný typ záznamu."


def run_interactive_loop(foods: list[FoodDefinition], conn: sqlite3.Connection) -> None:
    """Run interactive CLI chat loop."""
    print("Nutrition Agent spuštěn. Zadejte záznam stravy nebo hmotnosti.")
    print("Příkazy: 'dnes' pro denní přehled, 'historie' pro historii, 'konec' pro ukončení.")
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
        "--history",
        "-H",
        nargs="?",
        const=10,
        type=int,
        default=None,
        help="Zobrazit posledních N záznamů (výchozí 10)",
    )
    args = parser.parse_args()

    init_db(settings.database_path)
    conn = get_connection(settings.database_path)
    foods = load_foods_from_json(settings.foods_data_path)

    try:
        if args.today:
            print(format_daily_summary(conn))
        elif args.history is not None:
            print(format_history(conn, limit=args.history))
        elif args.message:
            full_message = " ".join(args.message)
            response = handle_user_message(full_message, foods, conn)
            print(response)
        else:
            run_interactive_loop(foods, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
