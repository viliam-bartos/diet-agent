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

        return cls(
            database_path=Path(db_path_str),
            foods_data_path=Path(foods_path_str),
            log_level=log_level.upper(),
            gemini_api_key=gemini_api_key,
        )

    def require_gemini_api_key(self) -> str:
        """Return GEMINI_API_KEY or fail fast if not set."""
        if not self.gemini_api_key or not self.gemini_api_key.strip():
            raise ValueError(
                "Nenalezen API klíč pro Gemini. Nastavte proměnnou prostředí GEMINI_API_KEY."
            )
        return self.gemini_api_key.strip()


settings = Settings.load()
