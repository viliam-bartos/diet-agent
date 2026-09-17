"""Command line interface for Nutrition Agent."""

import argparse
import re
import sqlite3
from datetime import datetime

from nutrition_agent.config import settings
from nutrition_agent.db import get_connection, init_db
from nutrition_agent.llm import parse_user_message
from nutrition_agent.models import (
    CalculatedWeightLog,
    CalculatedWorkoutLog,
    FoodDefinition,
    FoodNotFoundError,
    WeightValidationError,
)
from nutrition_agent.nutrition import (
    calculate_food_log,
    find_matching_food,
    validate_body_weight,
)
from nutrition_agent.reporting import format_daily_target_breakdown, generate_period_report
from nutrition_agent.repository import (
    get_daily_food_logs,
    get_recent_food_logs,
    get_recent_weight_logs,
    get_user_profile,
    load_foods_from_json,
    save_food_logs,
    save_weight_log,
    save_workout_log,
    set_user_setting,
)
from nutrition_agent.targets import (
    DayType,
    calculate_daily_targets,
    parse_day_type,
    parse_goal,
)


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
        save_workout_log(conn, workout_log)
        set_user_setting(conn, "day_type", DayType.TRAINING.value)
        dur_info = f" ({dur} min)" if dur else ""
        return (
            f"Zaznamenán trénink: {clean_desc}{dur_info}.\n"
            "Typ dne byl automaticky nastaven na TRAINING."
        )

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
        save_weight_log(conn, weight_log)

        goal, day_type, _ = get_user_profile(conn)
        new_targets = calculate_daily_targets(valid_weight, goal, day_type)
        return (
            f"Zaznamenána tělesná hmotnost: {valid_weight:.1f} kg\n"
            f"Cílové hodnoty byly automaticky přepočteny pro novou hmotnost ({goal.value}, "
            f"{day_type.value}): {new_targets.calories:.0f} kcal | "
            f"{new_targets.protein_usable:.1f} g DIAAS bílkovin | "
            f"{new_targets.carbs:.1f} g sacharidů | {new_targets.fats:.1f} g tuků."
        )

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
        for item in calculated_items:
            est_flag = " (odhad)" if item.is_estimated else ""
            state_flag = f" ({item.state})" if item.state == "cooked" else ""
            lines.append(
                f"- {item.food_name_matched} ({item.amount_g:.0f} g{state_flag}{est_flag}): "
                f"{item.calories:.1f} kcal | Bílkoviny: {item.protein_usable:.1f} g DIAAS "
                f"(celkem {item.protein_raw:.1f} g) | Sacharidy: {item.carbs:.1f} g | "
                f"Tuky: {item.fats:.1f} g"
            )

        today_prefix = datetime.now().strftime("%Y-%m-%d")
        daily_foods = get_daily_food_logs(conn, today_prefix)
        day_cal = sum(x.calories for x in daily_foods)
        day_prot = sum(x.protein_usable for x in daily_foods)
        day_carbs = sum(x.carbs for x in daily_foods)
        day_fats = sum(x.fats for x in daily_foods)

        goal, day_type, current_weight = get_user_profile(conn)
        targets = calculate_daily_targets(current_weight, goal, day_type)

        lines.append("---")
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
    args = parser.parse_args()

    # Standalone service runners
    if args.web or (args.message and args.message[0] == "web"):
        from nutrition_agent.web import run_server

        run_server()
        return

    if args.bot or (args.message and args.message[0] == "bot"):
        from nutrition_agent.bot import run_bot

        run_bot()
        return

    if args.run_all or (args.message and args.message[0] in ("run", "all")):
        import asyncio

        import uvicorn

        from nutrition_agent.bot import run_bot_async
        from nutrition_agent.web import create_app

        async def _run_both():
            config = uvicorn.Config(
                create_app(),
                host=settings.web_host,
                port=settings.web_port,
                log_level="info",
            )
            server = uvicorn.Server(config)
            await asyncio.gather(server.serve(), run_bot_async())

        asyncio.run(_run_both())
        return

    init_db(settings.database_path)
    conn = get_connection(settings.database_path)
    foods = load_foods_from_json(settings.foods_data_path)

    try:
        if args.goal:
            goal = parse_goal(args.goal)
            set_user_setting(conn, "goal", goal.value)
            print(f"Aktivní cíl nastaven na: {goal.value}")

        if args.day_type:
            day_type = parse_day_type(args.day_type)
            set_user_setting(conn, "day_type", day_type.value)
            print(f"Typ dne nastaven na: {day_type.value}")

        if args.today:
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
