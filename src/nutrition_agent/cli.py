"""Command line interface for Nutrition Agent."""

import argparse
import sqlite3

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
    load_foods_from_json,
    save_food_logs,
    save_weight_log,
)


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
            lines.append(
                f"- {item.food_name_matched} ({item.amount_g:.0f} g{est_flag}): "
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
    print("Nutrition Agent spuštěn. Zadejte záznam stravy nebo hmotnosti (konec pro ukončení).")
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
    args = parser.parse_args()

    init_db(settings.database_path)
    conn = get_connection(settings.database_path)
    foods = load_foods_from_json(settings.foods_data_path)

    try:
        if args.message:
            full_message = " ".join(args.message)
            response = handle_user_message(full_message, foods, conn)
            print(response)
        else:
            run_interactive_loop(foods, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
