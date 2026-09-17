"""Configuration settings for Nutrition Agent."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from environment variables."""

    database_path: Path
    foods_data_path: Path
    log_level: str
    gemini_api_key: str | None
    discord_bot_token: str | None = None
    discord_guild_id: int | None = None
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    target_calories: float | None = None
    target_protein_g: float | None = None

    @property
    def foods_db_path(self) -> Path:
        """Alias for foods_data_path."""
        return self.foods_data_path

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from environment with default fallbacks."""
        db_path_str = os.getenv("DATABASE_PATH", "data/nutrition.db")
        foods_path_str = os.getenv("FOODS_DB_PATH") or os.getenv(
            "FOODS_DATA_PATH", "data/foods.json"
        )
        log_level = os.getenv("LOG_LEVEL", "INFO")
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        discord_bot_token = os.getenv("DISCORD_BOT_TOKEN")
        guild_id_raw = os.getenv("DISCORD_GUILD_ID")
        discord_guild_id = int(guild_id_raw) if guild_id_raw and guild_id_raw.strip() else None
        web_host = os.getenv("WEB_HOST", "127.0.0.1")
        web_port = int(os.getenv("WEB_PORT", "8000"))

        target_cals_raw = os.getenv("TARGET_CALORIES")
        target_calories = float(target_cals_raw) if target_cals_raw else None

        target_prot_raw = os.getenv("TARGET_PROTEIN_G")
        target_protein_g = float(target_prot_raw) if target_prot_raw else None

        return cls(
            database_path=Path(db_path_str),
            foods_data_path=Path(foods_path_str),
            log_level=log_level.upper(),
            gemini_api_key=gemini_api_key,
            discord_bot_token=discord_bot_token,
            discord_guild_id=discord_guild_id,
            web_host=web_host,
            web_port=web_port,
            target_calories=target_calories,
            target_protein_g=target_protein_g,
        )

    def require_gemini_api_key(self) -> str:
        """Return GEMINI_API_KEY or fail fast if not set."""
        if not self.gemini_api_key or not self.gemini_api_key.strip():
            raise ValueError(
                "Nenalezen API klíč pro Gemini. Nastavte proměnnou prostředí GEMINI_API_KEY."
            )
        return self.gemini_api_key.strip()

    def require_discord_token(self) -> str:
        """Return DISCORD_BOT_TOKEN or fail fast if not set."""
        if not self.discord_bot_token or not self.discord_bot_token.strip():
            raise ValueError(
                "Nenalezen token pro Discord bota. Nastavte proměnnou prostředí DISCORD_BOT_TOKEN."
            )
        return self.discord_bot_token.strip()


settings = Settings.load()
