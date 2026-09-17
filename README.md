# Nutrition Agent

Minimalistický systém pro záznam stravy, maker a tělesné hmotnosti formou chatu (CLI, v budoucnu Discord bot).

Model Google Gemini Flash (přes oficiální SDK `google-genai`) slouží výhradně jako bezstavový parser pro extrakci entit z volného textu do striktního Pydantic schématu. Veškeré výpočty, kalorie a databázové operace probíhají deterministicky v lokálním kódu v Pythonu s diskontováním bílkovin podle DIAAS a ukládáním do lokální SQLite databáze.

---

## Architektura projektu

- `src/nutrition_agent/models.py`: Pydantic schémata (`UserLog`, `ExtractedFoodItem`, `FoodDefinition`, `CalculatedFoodLog`, `CalculatedWeightLog`) a doménové výjimky (`FoodNotFoundError`, `WeightValidationError`).
- `src/nutrition_agent/nutrition.py`: Výpočet využitelných bílkovin (DIAAS), přepočet jednotek na gramy, validace tělesné hmotnosti a fuzzy párování potravin (`rapidfuzz`).
- `src/nutrition_agent/llm.py`: Bezstavový parser volající Google Gemini Flash přes `google-genai` SDK se strukturovaným JSON výstupem.
- `src/nutrition_agent/db.py`: SQLite inicializace schématu a správa spojení.
- `src/nutrition_agent/repository.py`: Načítání katalogu potravin (`data/foods.json`) a ukládání/čtení záznamů v SQLite.
- `src/nutrition_agent/config.py`: Konfigurace aplikace a načítání proměnných prostředí.
- `src/nutrition_agent/cli.py`: Rozhraní příkazové řádky pro interaktivní režim i jednorázové zprávy.
- `data/foods.json`: Lokální katalog potravin s nutričními hodnotami a DIAAS koeficienty.
- `spec.md`: Technická specifikace datového toku, SQLite schématu a DIAAS koeficientů.
- `vault.md`: Referenční tabulka surovin, převodní jednotky a pravidla pro fuzzy matching.

---

## Požadavky

- Python >= 3.12
- API klíč pro Google Gemini (`GEMINI_API_KEY`)

---

## Instalace a nastavení

1. Přejdi do kořenového adresáře projektu:
   ```bash
   cd diet-agent
   ```

2. Vytvoř a aktivuj virtuální prostředí:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .venv\Scripts\Activate.ps1
   # Linux/macOS:
   source .venv/bin/activate
   ```

3. Nainstaluj závislosti včetně vývojových:
   ```bash
   pip install -e ".[dev]"
   ```

4. Vytvoř konfigurační soubor `.env`:
   ```bash
   cp .env.example .env
   ```
   Do souboru `.env` doplň svůj klíč:
   ```env
   GEMINI_API_KEY=tvuj-gemini-api-klic
   DATABASE_PATH=data/nutrition.db
   FOODS_DATA_PATH=data/foods.json
   LOG_LEVEL=INFO
   ```

---

## Použití

### Interaktivní režim

Spuštění interaktivního CLI chatu:
```bash
python -m nutrition_agent.cli
# nebo po instalaci balíčku:
nutrition-agent
```

Příklady vstupů:
- `Dneska k obědu 200g kuřecích prsou a 150g rýže`
- `Váha 82.4 kg`
- `2 vejce a 1 banán`

Ukončení interaktivního režimu zadáním `konec`, `exit` nebo `quit`.

### Jednorázový záznam

Zprávu lze předat přímo jako argument:
```bash
nutrition-agent "150g hovězího a 200g brambor"
```

---

## DIAAS diskontování bílkovin

Hrubý příjem bílkovin z etiket neodpovídá množství aminokyselin, které tělo skutečně využije. Systém diskontuje bílkoviny podle vzorce:

```
Protein_usable = Protein_raw * Koeficient
```

Koeficienty stravitelnosti:
- Živočišné zdroje (maso, vejce, syrovátkový protein, tvaroh): 1.00
- Rostlinné izoláty a tofu: 0.85
- Luštěniny (čočka, cizrna): 0.75
- Obiloviny a semena (ovesné vločky, rýže, těstoviny): 0.70
- Ořechy a arašídové máslo: 0.65
- Výchozí hodnota pro neurčené suroviny: 0.70

---

## Testy a kontrola kvality kódu

Spuštění testů:
```bash
pytest
```

Kontrola linterem a formátování:
```bash
ruff check .
ruff format --check .
```
