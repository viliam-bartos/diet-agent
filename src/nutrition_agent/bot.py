"""Discord bot implementation using discord.py and modern Slash Commands."""

import logging
from datetime import datetime

import discord
from discord import app_commands

from nutrition_agent.cli import handle_user_message
from nutrition_agent.config import settings
from nutrition_agent.db import get_connection, init_db
from nutrition_agent.models import CalculatedWeightLog, CalculatedWorkoutLog, WeightValidationError
from nutrition_agent.nutrition import validate_body_weight
from nutrition_agent.reporting import generate_period_report
from nutrition_agent.repository import (
    get_daily_food_logs,
    get_daily_weight_comparison,
    get_daily_workout_log,
    get_user_profile,
    load_foods_from_json,
    save_weight_log,
    save_workout_log,
    set_user_setting,
)
from nutrition_agent.targets import DayType, calculate_daily_targets

logger = logging.getLogger(__name__)


def render_discord_progress_bar(current: float, target: float, width: int = 12) -> str:
    """Render Unicode block progress bar [████░░░░] for Discord Embeds."""
    if target <= 0:
        return f"[{'░' * width}] 0 %"
    ratio = min(max(current / target, 0.0), 1.5)
    filled = int(round(ratio * width))
    bar_fill = min(filled, width)
    bar_empty = width - bar_fill
    bar = "█" * bar_fill + "░" * bar_empty
    pct = int(round((current / target) * 100.0))
    return f"[{bar}] {pct} %"


def build_dashboard_view() -> discord.ui.View:
    """Build persistent Discord UI button opening the web dashboard."""
    view = discord.ui.View()
    web_url = f"http://{settings.web_host}:{settings.web_port}"
    button = discord.ui.Button(
        label="Otevřít webový dashboard",
        url=web_url,
        style=discord.ButtonStyle.link,
    )
    view.add_item(button)
    return view


def create_bot() -> tuple[discord.Client, app_commands.CommandTree]:
    """Create and configure discord.Client and app_commands.CommandTree."""
    # Schema creation is idempotent and upgrades databases created by older versions.
    init_db(settings.database_path)

    intents = discord.Intents.default()
    intents.message_content = True

    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    @client.event
    async def on_ready():
        await tree.sync()
        logger.info("Discord bot prihlasen jako %s a slash prikazy synchronizovany.", client.user)

    @client.event
    async def on_message(message: discord.Message):
        # Ignore bot messages or slash command invocations
        if message.author.bot or message.content.startswith("/"):
            return

        clean_text = message.content.strip()
        if not clean_text:
            return

        conn = get_connection(settings.database_path)
        foods = load_foods_from_json(settings.foods_data_path)

        try:
            reply = handle_user_message(clean_text, foods, conn)
            is_error = reply.startswith("Chyba")
            color = discord.Color.red() if is_error else discord.Color.green()

            embed = discord.Embed(
                title="Záznam stravy & telemetrie",
                description=f"```text\n{reply}\n```",
                color=color,
                timestamp=datetime.now(),
            )
            embed.set_footer(text=f"Web dashboard: http://{settings.web_host}:{settings.web_port}")
            await message.channel.send(embed=embed, view=build_dashboard_view())
        finally:
            conn.close()

    @tree.command(name="dnes", description="Mobilní denní přehled stravy, maker a tréninku")
    async def cmd_today(interaction: discord.Interaction):
        conn = get_connection(settings.database_path)
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")

            # 1. Weight & Delta against previous logged day
            w_today, w_diff = get_daily_weight_comparison(conn, today_str)

            if w_today:
                diff_str = f" ({'+' if w_diff >= 0 else ''}{w_diff:.1f} kg)" if w_diff else ""
                weight_val = f"{w_today.weight_kg:.1f} kg{diff_str}"
            else:
                weight_val = "Zatím nezaznamenána"

            # 2. Workout status
            workout = get_daily_workout_log(conn, today_str)
            goal, day_type, current_weight = get_user_profile(conn)
            if workout:
                dur_str = f" ({workout.duration_minutes} min)" if workout.duration_minutes else ""
                workout_val = f"{workout.workout_type}{dur_str}"
            elif day_type == DayType.REST:
                workout_val = "Rest Day"
            else:
                workout_val = "Plánován trénink (zatím nezapsán)"

            # 3. Targets & Consumption
            targets = calculate_daily_targets(current_weight, goal, day_type)
            food_logs = get_daily_food_logs(conn, today_str)

            total_cal = sum(x.calories for x in food_logs)
            total_prot = sum(x.protein_usable for x in food_logs)
            total_carbs = sum(x.carbs for x in food_logs)
            total_fats = sum(x.fats for x in food_logs)

            bar_cal = render_discord_progress_bar(total_cal, targets.calories, width=12)
            bar_prot = render_discord_progress_bar(total_prot, targets.protein_usable, width=12)

            macros_text = (
                f"Kalorie:   {bar_cal}  ({total_cal:.0f} / {targets.calories:.0f} kcal)\n"
                f"Bílkoviny: {bar_prot}  ({total_prot:.1f} / {targets.protein_usable:.1f} g)\n"
                f"Sacharidy: {total_carbs:.0f} g / {targets.carbs:.0f} g  |  "
                f"Tuky: {total_fats:.0f} g / {targets.fats:.0f} g"
            )

            # 4. Meal list
            if food_logs:
                meal_lines = []
                for f in food_logs:
                    time_p = f.logged_at[11:16] if len(f.logged_at) >= 16 else "--:--"
                    meal_lines.append(
                        f"• `{time_p}` {f.food_name_matched} ({f.amount_g:.0f}g): "
                        f"{f.calories:.0f} kcal, {f.protein_usable:.1f}g DIAAS"
                    )
                meals_text = "\n".join(meal_lines)
            else:
                meals_text = "Dnes zatím nebyla zaznamenána žádná jídla."

            embed = discord.Embed(
                title=f"Denní přehled ({today_str})",
                color=discord.Color.blue(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="Hmotnost", value=weight_val, inline=True)
            embed.add_field(name="Tréninkový status", value=workout_val, inline=True)
            embed.add_field(
                name="Aktivní cíl",
                value=f"{goal.value.upper()} ({day_type.value})",
                inline=True,
            )
            embed.add_field(
                name="Makra & Energie",
                value=f"```text\n{macros_text}\n```",
                inline=False,
            )
            embed.add_field(name="Dnešní jídla", value=meals_text, inline=False)
            embed.set_footer(text=f"Web dashboard: http://{settings.web_host}:{settings.web_port}")

            await interaction.response.send_message(embed=embed, view=build_dashboard_view())
        finally:
            conn.close()

    @tree.command(name="vaha", description="Okamžitý záznam ranní tělesné hmotnosti v kg")
    @app_commands.describe(hodnota="Tělesná hmotnost v kg (např. 83.4)")
    async def cmd_weight(interaction: discord.Interaction, hodnota: float):
        try:
            valid_w = validate_body_weight(hodnota)
        except WeightValidationError as exc:
            await interaction.response.send_message(f"Chyba validace: {exc}", ephemeral=True)
            return

        conn = get_connection(settings.database_path)
        try:
            log = CalculatedWeightLog(
                raw_input=f"/vaha {hodnota}",
                weight_kg=valid_w,
            )
            save_weight_log(conn, log)
            goal, day_type, _ = get_user_profile(conn)
            targets = calculate_daily_targets(valid_w, goal, day_type)

            embed = discord.Embed(
                title="Hmotnost zaznamenána",
                description=(
                    f"Nová tělesná hmotnost: **{valid_w:.1f} kg**\n"
                    f"Cíle automaticky přepočteny pro {goal.value} ({day_type.value}):\n"
                    f"**{targets.calories:.0f} kcal** | **{targets.protein_usable:.1f} g DIAAS** | "
                    f"**{targets.carbs:.0f} g sacharidů** | **{targets.fats:.0f} g tuků**"
                ),
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed, view=build_dashboard_view())
        finally:
            conn.close()

    @tree.command(name="trenink", description="Rychlý záznam odtrénovaného tréninku")
    @app_commands.describe(
        typ="Typ tréninku (např. Silový trénink, Běh, Nohy)",
        delka_min="Délka tréninku v minutách (např. 60)",
        poznamka="Poznámka k tréninku",
    )
    async def cmd_workout(
        interaction: discord.Interaction,
        typ: str = "Silový trénink",
        delka_min: int = 60,
        poznamka: str = "",
    ):
        conn = get_connection(settings.database_path)
        try:
            log = CalculatedWorkoutLog(
                raw_input=f"/trenink {typ} {delka_min} min {poznamka}",
                workout_type=typ,
                duration_minutes=delka_min,
                notes=poznamka or None,
            )
            save_workout_log(conn, log)
            set_user_setting(conn, "day_type", DayType.TRAINING.value)

            embed = discord.Embed(
                title="Trénink zaznamenán",
                description=(
                    f"Trénink: **{typ}** ({delka_min} min)\n"
                    f"{'Poznámka: ' + poznamka if poznamka else ''}\n"
                    "Typ dne byl automaticky nastaven na **TRAINING**."
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, view=build_dashboard_view())
        finally:
            conn.close()

    @tree.command(name="prehled", description="Rychlá tabulka a souhrn za zvolené období")
    @app_commands.describe(obdobi="Časové období: tyden, mesic, pulrok, rok, vse")
    async def cmd_summary(interaction: discord.Interaction, obdobi: str = "tyden"):
        conn = get_connection(settings.database_path)
        try:
            report_text = generate_period_report(conn, obdobi)
            embed = discord.Embed(
                title=f"Přehled za období: {obdobi.upper()}",
                description=f"```text\n{report_text}\n```",
                color=discord.Color.teal(),
            )
            await interaction.response.send_message(embed=embed, view=build_dashboard_view())
        finally:
            conn.close()

    @tree.command(name="web", description="Přímý odkaz na webový dashboard")
    async def cmd_web(interaction: discord.Interaction):
        web_url = f"http://{settings.web_host}:{settings.web_port}"
        embed = discord.Embed(
            title="Webový dashboard",
            description=f"Otevřete webový dashboard na adrese:\n<{web_url}>",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, view=build_dashboard_view())

    return client, tree


async def run_bot_async() -> None:
    """Run Discord bot client asynchronously."""
    token = settings.require_discord_token()
    client, _ = create_bot()
    await client.start(token)


def run_bot() -> None:
    """Synchronous entry point to run Discord bot."""
    import asyncio

    asyncio.run(run_bot_async())


if __name__ == "__main__":
    run_bot()
