"""Stateless LLM parser using Google Gemini Flash and google-genai SDK."""

import re

from google import genai
from google.genai import types

from nutrition_agent.config import settings
from nutrition_agent.models import (
    EstimatedFoodNutrition,
    EstimatedUnitConversion,
    UserLog,
)

SYSTEM_INSTRUCTION = """Jsi striktní parser uživatelských zpráv o stravě a tělesné hmotnosti.
Tvým jediným úkolem je extrahovat entity a vrátit je v JSON podle schématu UserLog.

Pravidla chování:
1. Zákaz kalkulací: Nikdy neprováděj výpočty ani nedohledávej kalorie a makroživiny.
2. Čistá extrakce: Z textu extrahuj suroviny, množství, jednotku, stav a odhad.
3. Chybějící množství se ODHADUJE, nikdy kvůli němu nevracej 'INVALID':
   - Uvedl-li uživatel číslo, použij přesně je a nastav is_estimated=False.
   - Neuvedl-li číslo, zvol typickou jednotku a počet a nastav is_estimated=True:
     - Kusové potraviny ('jablko', 'banán', '2 vejce') -> amount: 1, unit: 'ks'.
     - Hotová jídla a restaurační porce ('svíčková', 'guláš s knedlíkem', 'butter chicken')
       -> amount: 1, unit: 'porce', state: 'cooked'.
     - Suroviny, u kterých uživatel míru vynechal ('rýže', 'kuřecí', 'těstoviny')
       -> amount: 1, unit: 'porce', state: 'cooked'.
     - Nápoje vyjadřuj vždy v mililitrech, nikdy v kusech, a počet vynásob:
       pivo -> 500 ml za jedno; víno -> 200 ml; panák destilátu -> 40 ml;
       káva nebo čaj -> 200 ml; limonáda -> 330 ml.
       'dal jsem si pivo' -> amount: 500, unit: 'ml'
       'vypil jsem dvě piva' -> amount: 1000, unit: 'ml'
       'dvě kávy' -> amount: 400, unit: 'ml'
     - Míry, které nejsou v seznamu jednotek ('hrst', 'sáček', 'kelímek', 'kopeček'),
       namapuj na 'ks', jde-li o jednu porci, jinak na 'porce'.
   Nikdy neurčuj gramy sám, jen zvol jednotku a počet. Hmotnost dopočítá lokální kód.
4. Stav úpravy: U hotových jídel nastavuj state='cooked'. U masa pouze jedlá část bez kostí.
5. Příznak odhadu (is_estimated): Nastav is_estimated=True, pokud množství nebylo v textu
   uvedeno číslem. Pokud uživatel zadal přesné číslo (např. '150g rýže'), nastav is_estimated=False.
6. Normalizace: Názvy potravin normalizuj do základního tvaru v češtině v jednotném čísle.
   Názvy cizích jídel ponech v originále nebo je přelož ('chicken butter' -> 'butter chicken').
7. Jednotky: Zachovej míru, kterou uživatel skutečně použil. Neměň ji na 'ks' ani na gramy.
   - 'g' nebo 'ml' pro vážené a tekuté suroviny ('150g rýže', '200ml mléka').
   - 'odměrka' pro odměrky ('2 odměrky proteinu' -> amount: 2, unit: 'odměrka').
   - 'lžíce', 'lžička' pro lžíce ('lžíce olivového oleje' -> amount: 1, unit: 'lžíce').
   - 'hrnek' pro hrnky, 'plátek' pro plátky ('2 plátky šunky'), 'krajíc' pro krajíce chleba.
   - 'ks' pro počitatelné kusy ('2 vejce', '1 banán').
   - 'porce' pro hotová jídla na talíři ('svíčková' -> amount: 1, unit: 'porce').
   Nikdy neuváděj gramy, pokud je uživatel neuvedl. Převod na gramy řeší lokální kód.
8. Tělesná hmotnost:
   - Pokud uživatel hlásí tělesnou váhu (např. 'Váha 83 kg', 'Vážím 82.5', 'Dnes 84.1kg'):
     nastav log_type: 'WEIGHT' a do pole weight_kg vlož numerickou hodnotu váhy v kg jako float.
     Pole items MUSÍ být prázdný seznam [].
9. Neplatné vstupy: 'INVALID' použij POUZE když vstup neobsahuje jídlo ani váhu, nebo je
   nesrozumitelný (např. 'ahoj', 'asdfgh'). Nikdy kvůli chybějícímu množství.
10. Formát výstupu: Zákaz jakéhokoliv doplňkového textu mimo validní JSON schématu.

Příklady očekávaných JSON výstupů:
Vstup: "Váha 83 kg"
Výstup: {"log_type": "WEIGHT", "weight_kg": 83.0, "items": []}

Vstup: "200g kuřecích prsou a 150g rýže"
Výstup: {"log_type": "FOOD", "items": [{"name": "kuřecí prsa", "amount": 200.0, "unit": "g"}]}

Vstup: "Měl jsem jablko"
Výstup: {"log_type": "FOOD", "items": [{"name": "jablko", "amount": 1.0, "unit": "ks"}]}

Vstup: "Dal jsem si 2 odměrky proteinu"
Výstup: {"log_type": "FOOD", "items": [{"name": "syrovátkový protein",
  "amount": 2.0, "unit": "odměrka"}]}

Vstup: "Lžíce olivového oleje a 2 plátky šunky"
Výstup: {"log_type": "FOOD", "items": [
  {"name": "olivový olej", "amount": 1.0, "unit": "lžíce"},
  {"name": "šunka", "amount": 2.0, "unit": "plátek"}]}

Vstup: "Měl jsem rýži"
Výstup: {"log_type": "FOOD", "items": [{"name": "rýže", "amount": 1.0,
  "unit": "porce", "state": "cooked"}]}

Vstup: "Měl jsem chicken butter v indické restauraci, k tomu porci rýže a placku"
Výstup: {"log_type": "FOOD", "items": [
  {"name": "butter chicken", "amount": 1.0, "unit": "porce", "state": "cooked"},
  {"name": "rýže", "amount": 1.0, "unit": "porce", "state": "cooked"},
  {"name": "indická placka", "amount": 1.0, "unit": "ks", "state": "cooked"}]}

Vstup: "ahoj jak se máš"
Výstup: {"log_type": "INVALID", "error_reason": "Vstup neobsahuje jídlo ani váhu"}

Vstup: "dal jsem si pivo"
Výstup: {"log_type": "FOOD", "items": [{"name": "pivo", "amount": 500.0,
  "unit": "ml", "state": "raw"}]}
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


NUTRITION_ESTIMATE_INSTRUCTION = """Jsi nutriční databáze. Uživatel zadal potravinu, která není
v jeho lokálním katalogu. Odhadni její nutriční hodnoty na 100 g v syrovém / suchém stavu.

Pravidla:
1. Vrátíš pouze JSON podle schématu EstimatedFoodNutrition.
2. Hodnoty uváděj na 100 g. U tekutin na 100 ml.
3. Vycházej z typických hodnot dané potraviny. Pokud jde o hotové jídlo, uváděj hodnoty
   hotového jídla tak, jak se jí.
4. Kategorie bílkovin (protein_category):
   - 'animal': maso, ryby, vejce, mléčné výrobky, syrovátkový protein
   - 'plant_isolate': sójový nebo hrachový izolát, tofu
   - 'legumes': čočka, fazole, cizrna
   - 'grains_seeds': obiloviny, rýže, těstoviny, chléb, semena
   - 'nuts_peanuts': ořechy a arašídy
   - 'unknown': pokud si kategorií nejsi jistý
5. Alkohol: u piva, vína, destilátů a míchaných nápojů vyplň 'alcohol' čistým etanolem
   v gramech na 100 ml nebo 100 g. Etanol má 7 kcal/g a do makroživin nepatří.
   Přibližně: 4 % obj. = 3,2 g/100 ml, 5 % = 4 g, 12 % = 9,6 g, 40 % = 32 g.
   U nealkoholických potravin nech 0.
6. Energetická bilance musí odpovídat:
   kcal ≈ bílkoviny*4 + sacharidy*4 + tuky*9 + alkohol*7.
   U piva tak energie pochází hlavně z alkoholu, ne ze sacharidů.
7. Pokud potravinu neznáš nebo je nesmyslná, vrať nuly a protein_category 'unknown'.
8. Zákaz jakéhokoliv textu mimo JSON.

Příklady:
Vstup: "kuřecí kari"
Výstup: {"name": "kuřecí kari", "calories": 130, "protein": 12, "carbs": 6, "fats": 6,
         "alcohol": 0, "protein_category": "animal"}

Vstup: "quinoa"
Výstup: {"name": "quinoa", "calories": 368, "protein": 14, "carbs": 64, "fats": 6,
         "alcohol": 0, "protein_category": "grains_seeds"}

Vstup: "ležák 12°"
Výstup: {"name": "ležák 12°", "calories": 43, "protein": 0.5, "carbs": 3.5, "fats": 0,
         "alcohol": 4.0, "protein_category": "grains_seeds"}

Vstup: "víno bílé"
Výstup: {"name": "víno bílé", "calories": 82, "protein": 0.1, "carbs": 2.6, "fats": 0,
         "alcohol": 9.6, "protein_category": "unknown"}
"""


def estimate_food_nutrition(
    food_name: str,
    client: genai.Client | None = None,
    model: str = "gemini-3.6-flash",
) -> EstimatedFoodNutrition:
    """Ask the model for nutrition values of a food missing from the local catalog.

    The result is an estimate, never a measurement. Callers must store and display
    it as such. This function performs no calculations and writes nothing.
    """
    clean_name = food_name.strip()
    if not clean_name:
        raise ValueError("Nelze odhadnout nutriční hodnoty pro prázdný název potraviny.")

    active_client = client or create_gemini_client()

    config = types.GenerateContentConfig(
        system_instruction=NUTRITION_ESTIMATE_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=EstimatedFoodNutrition,
        temperature=0.0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    response = active_client.models.generate_content(
        model=model,
        contents=clean_name,
        config=config,
    )

    if isinstance(response.parsed, EstimatedFoodNutrition):
        return response.parsed

    if response.text:
        return EstimatedFoodNutrition.model_validate_json(response.text)

    raise ValueError(f"Model nevrátil odhad pro potravinu '{food_name}'.")


UNIT_CONVERSION_INSTRUCTION = """Jsi převodník kuchyňských měr na gramy.
Dostaneš potravinu, jednotku a stav, ve kterém ji uživatel snědl. Odpověz, kolik gramů
odpovídá jedné jednotce TAK, JAK JI UŽIVATEL SNĚDL.

Pravidla:
1. Vrátíš pouze JSON podle schématu EstimatedUnitConversion.
2. Počítej jednu jednotku, ne množství, které uživatel zadal.
3. Hmotnost uváděj v podobě, v jaké se jídlo jí, ne v surovině. Na stavu záleží:
   - state='cooked' znamená hotové jídlo na talíři. U rýže, těstovin a luštěnin uváděj
     hmotnost VAŘENÝCH, u masa hmotnost UPEČENÉHO. Vařená rýže má zhruba 2,5x vyšší
     hmotnost než suchá, vařené těstoviny zhruba 2,3x.
   - state='raw' znamená syrovou nebo suchou surovinu.
   Nikdy nemíchej obojí: 'porce' vařené rýže není totéž co 'porce' suché rýže.
4. Typické hodnoty:
   - 'odměrka' proteinu je obvykle 30 g, u konkrétního produktu se může lišit.
   - 'porce' hotového jídla v restauraci je obvykle 250-400 g.
   - 'porce' vařené přílohy (rýže, těstoviny, brambory) je obvykle 200-250 g.
   - 'plátek' šunky 15-25 g, 'krajíc' chleba 35-50 g, 'ks' placky 80-120 g.
   - 'hrnek' je obvykle 200-250 g podle potraviny.
5. Uveď hodnotu v gramech jako číslo. Nepoužívej rozsahy ani text.
6. Pokud jednotku u dané potraviny neznáš, vrať 0.

Příklady:
Vstup: "syrovátkový protein | odměrka | raw"
Výstup: {"name": "syrovátkový protein", "unit": "odměrka", "grams_per_unit": 30}

Vstup: "Rýže jasmínová (suchá) | porce | cooked"
Výstup: {"name": "Rýže jasmínová (suchá)", "unit": "porce", "grams_per_unit": 220}

Vstup: "Rýže jasmínová (suchá) | porce | raw"
Výstup: {"name": "Rýže jasmínová (suchá)", "unit": "porce", "grams_per_unit": 80}

Vstup: "svíčková na smetaně | porce | cooked"
Výstup: {"name": "svíčková na smetaně", "unit": "porce", "grams_per_unit": 350}
"""


def estimate_unit_conversion(
    food_name: str,
    unit: str,
    state: str = "raw",
    client: genai.Client | None = None,
    model: str = "gemini-3.6-flash",
) -> EstimatedUnitConversion:
    """Ask the model how many grams one unit of a food weighs as it was eaten.

    ``state`` matters: a portion of dry rice is about 80 g while a portion of cooked
    rice is about 220 g. Without it the model answers about the raw catalog form and
    the cooked/raw ratio then discounts the weight a second time.

    The result is an estimate and a suggestion only; callers must store it with an
    'ai' source and keep it visible and correctable. An unknown unit must come back
    as zero so the caller can refuse instead of inventing a weight.
    """
    clean_name = food_name.strip()
    clean_unit = unit.strip().lower()
    clean_state = state.strip().lower() or "raw"
    if not clean_name:
        raise ValueError("Nelze odhadnout převod pro prázdný název potraviny.")
    if not clean_unit:
        raise ValueError("Nelze odhadnout převod pro prázdnou jednotku.")

    active_client = client or create_gemini_client()

    config = types.GenerateContentConfig(
        system_instruction=UNIT_CONVERSION_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=EstimatedUnitConversion,
        temperature=0.0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    response = active_client.models.generate_content(
        model=model,
        contents=f"{clean_name} | {clean_unit} | {clean_state}",
        config=config,
    )

    if isinstance(response.parsed, EstimatedUnitConversion):
        return response.parsed

    if response.text:
        return EstimatedUnitConversion.model_validate_json(response.text)

    raise ValueError(f"Model nevrátil převod pro '{clean_unit}' u '{food_name}'.")
