"""Stateless LLM parser using Google Gemini Flash and google-genai SDK."""

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
7. Tělesná hmotnost: Pokud uživatel hlásí tělesnou váhu, nastav log_type='WEIGHT' a weight_kg.
8. Neplatné vstupy: Pokud je vstup nesrozumitelný nebo chybí množství, nastav log_type='INVALID'
   a do error_reason uveď důvod.
9. Formát výstupu: Zákaz jakéhokoliv doplňkového textu mimo validní JSON schématu.
"""


def create_gemini_client(api_key: str | None = None) -> genai.Client:
    """Instantiate Google GenAI client with validated API key."""
    resolved_key = api_key or settings.require_gemini_api_key()
    return genai.Client(api_key=resolved_key)


def parse_user_message(
    message: str,
    client: genai.Client | None = None,
    model: str = "gemini-2.5-flash",
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
    )

    response = active_client.models.generate_content(
        model=model,
        contents=clean_message,
        config=config,
    )

    if isinstance(response.parsed, UserLog):
        return response.parsed

    if response.text:
        return UserLog.model_validate_json(response.text)

    return UserLog(
        log_type="INVALID",
        error_reason="Model nevrátil žádná data.",
    )
