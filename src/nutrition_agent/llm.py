"""Stateless LLM parser using Google Gemini Flash and google-genai SDK."""

import re

from google import genai
from google.genai import types

from nutrition_agent.config import settings
from nutrition_agent.models import UserLog

SYSTEM_INSTRUCTION = """Jsi striktní parser uživatelských zpráv o stravě a tělesné hmotnosti.
Tvým jediným úkolem je extrahovat entity a vrátit je v JSON podle schématu UserLog.

Pravidla chování:
1. Zákaz kalkulací: Nikdy neprováděj výpočty ani nedohledávej kalorie a makroživiny.
2. Čistá extrakce: Z textu extrahuj suroviny, množství, jednotku, stav a odhad.
3. Ošetření chybějícího množství:
   - Kusové potraviny (např. 'jablko', 'banán', '2 vejce'):
     pokud není uvedeno číslo, nastav amount: 1, unit: 'ks', is_estimated: True.
   - Vážené / nespočitatelné suroviny (např. 'rýže', 'kuřecí'):
     pokud chybí množství nebo porce, nastav log_type: 'INVALID' a
     error_reason: 'Nespecifikované množství suroviny'.
   - Běžná hotová jídla na porce (např. 'svíčková'):
     nastav amount: 1, unit: 'porce', is_estimated: True, state: 'cooked'.
4. Stav úpravy: U hotových jídel nastavuj state='cooked'. U masa pouze jedlá část bez kostí.
5. Příznak odhadu (is_estimated): Nastav is_estimated=True, pokud gramáž nebyla v textu
   uvedena číslem. Pokud uživatel zadal přesné číslo (např. '150g rýže'), nastav is_estimated=False.
6. Normalizace: Názvy potravin normalizuj do základního tvaru v češtině v jednotném čísle.
7. Tělesná hmotnost:
   - Pokud uživatel hlásí tělesnou váhu (např. 'Váha 83 kg', 'Vážím 82.5', 'Dnes 84.1kg'):
     nastav log_type: 'WEIGHT' a do pole weight_kg vlož numerickou hodnotu váhy v kg jako float.
     Pole items MUSÍ být prázdný seznam [].
8. Neplatné vstupy: Pokud je vstup nesrozumitelný nebo chybí množství u vážené suroviny,
   nastav log_type: 'INVALID' a do error_reason uveď důvod.
9. Formát výstupu: Zákaz jakéhokoliv doplňkového textu mimo validní JSON schématu.

Příklady očekávaných JSON výstupů:
Vstup: "Váha 83 kg"
Výstup: {"log_type": "WEIGHT", "weight_kg": 83.0, "items": []}

Vstup: "200g kuřecích prsou a 150g rýže"
Výstup: {"log_type": "FOOD", "items": [{"name": "kuřecí prsa", "amount": 200.0, "unit": "g"}]}

Vstup: "Měl jsem jablko"
Výstup: {"log_type": "FOOD", "items": [{"name": "jablko", "amount": 1.0, "unit": "ks"}]}

Vstup: "Měl jsem rýži"
Výstup: {"log_type": "INVALID", "error_reason": "Nespecifikované množství suroviny"}
"""


def create_gemini_client(api_key: str | None = None) -> genai.Client:
    """Instantiate Google GenAI client with validated API key."""
    resolved_key = api_key or settings.require_gemini_api_key()
    return genai.Client(api_key=resolved_key)


def _extract_weight_fallback(text: str) -> float | None:
    """Extract numeric weight value from user message via regex fallback."""
    pattern = (
        r"(?:váha|vazim|vážím|hmotnost|dnes)?\s*"
        r"([3-9]\d(?:\.\d+)?|[12]\d{2}(?:\.\d+)?)\s*(?:kg|kilo)?"
    )
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except (ValueError, IndexError):
            return None
    return None


def parse_user_message(
    message: str,
    client: genai.Client | None = None,
    model: str = "gemini-3.6-flash",
) -> UserLog:
    """Parse user text into structured UserLog using Gemini Flash.

    This function is strictly stateless and acts solely as an entity extractor.
    It performs zero mathematical calculations and zero persistence operations.
    """
    clean_message = message.strip()
    if not clean_message:
        return UserLog(
            log_type="INVALID",
            error_reason="Prázdný vstup.",
        )

    active_client = client or create_gemini_client()

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=UserLog,
        temperature=0.0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    try:
        response = active_client.models.generate_content(
            model=model,
            contents=clean_message,
            config=config,
        )
    except Exception as exc:
        err_str = str(exc)
        if "401" in err_str or "UNAUTHENTICATED" in err_str:
            raise RuntimeError(
                "Neplatný API klíč pro Gemini v souboru .env (kód 401 UNAUTHENTICATED).\n"
                "Ujistěte se, že používáte oficiální API klíč z "
                "https://aistudio.google.com/app/apikey (začíná prefixem 'AIzaSy...')."
            ) from exc
        raise

    parsed_log: UserLog | None = None
    if isinstance(response.parsed, UserLog):
        parsed_log = response.parsed
    elif response.text:
        parsed_log = UserLog.model_validate_json(response.text)

    if parsed_log is not None:
        # Fallback if model classified as WEIGHT but missed weight_kg
        if parsed_log.log_type == "WEIGHT" and parsed_log.weight_kg is None:
            fallback_weight = _extract_weight_fallback(clean_message)
            if fallback_weight is not None:
                parsed_log.weight_kg = fallback_weight
        return parsed_log

    return UserLog(
        log_type="INVALID",
        error_reason="Model nevrátil žádná data.",
    )
