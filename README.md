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
- `Váha 83.2 kg` (automaticky přepočítá nutriční cíle pro novou váhu)
- `2 vejce a 1 banán`
- `dnes` nebo `prehled` pro zobrazení denního souhrnu, plnění cílů a zbývajících hodnot
- `tyden`, `mesic`, `pulrok`, `rok`, `vse` pro agregované přehledy s grafy
- `cil bulk`, `cil maintenance`, `cil cut` pro změnu aktivního stavu
- `den trenink` nebo `den odpocinek` pro přepnutí tréninkového stavu
- `historie` pro zobrazení posledních záznamů

Ukončení interaktivního režimu zadáním `konec`, `exit` nebo `quit`.

### Jednorázový záznam a přepínače

Zprávu lze předat přímo jako argument:
```bash
nutrition-agent "150g hovězího a 200g brambor"
```

Zobrazení přehledů podle období:
```bash
nutrition-agent --today       # Dnešní plnění cílů a chybějící živiny
nutrition-agent --week        # Týdenní souhrn (posledních 7 dní)
nutrition-agent --month       # Měsíční souhrn (posledních 30 dní)
nutrition-agent --half-year   # Půlroční souhrn (posledních 180 dní)
nutrition-agent --year        # Roční souhrn (posledních 365 dní)
nutrition-agent --all         # Kompletní souhrn ze všech dat
```

Nastavení aktivního cíle a typu dne:
```bash
nutrition-agent --goal lean_bulk   # Možnosti: lean_bulk, maintenance, cut
nutrition-agent --day-type training # Možnosti: training, rest
```

Zobrazení historie posledních N záznamů:
```bash
nutrition-agent --history 10
```

---

## Nutriční stavy a dynamický přepočet na váhu

Systém počítá energetickou bilanci a makroživiny dynamicky podle aktuální tělesné hmotnosti (kalibrováno na 83 kg referenční váhy):

1. **Lean Bulk (čisté nabírání)**:
   - Tréninkový den: ~3 150 kcal, Bílkoviny: ~155 g DIAAS (~1.87 g/kg), Tuky: ~82.5 g (~1.0 g/kg), Sacharidy: ~430 g (~5.18 g/kg)
   - Netréninkový den: ~2 650 kcal, Bílkoviny: ~145 g DIAAS (~1.75 g/kg), Tuky: ~77.5 g (~0.93 g/kg), Sacharidy: ~340 g (~4.10 g/kg)

2. **Maintenance (udržování váhy)**:
   - Tréninkový den: ~2 750 kcal, Bílkoviny: ~150 g DIAAS (~1.81 g/kg), Tuky: ~82.5 g, Sacharidy: ~320 g
   - Netréninkový den: ~2 475 kcal, Bílkoviny: ~145 g DIAAS (~1.75 g/kg), Tuky: ~80.0 g, Sacharidy: ~310 g

3. **Cut (rýsování / redukce)**:
   - Tréninkový den: ~2 200 kcal, Bílkoviny: ~175 g DIAAS (~2.11 g/kg), Tuky: ~68.0 g, Sacharidy: ~215 g
   - Netréninkový den: ~2 150 kcal, Bílkoviny: ~175 g DIAAS (~2.11 g/kg), Tuky: ~65.0 g, Sacharidy: ~205 g

Při zaznamenání nové tělesné hmotnosti se všechny cíle okamžitě a automaticky přepočítají.

---

## Přepočet tepelné úpravy (raw vs cooked)

Katalog potravin `data/foods.json` uvádí nutriční hodnoty v syrovém / suchém stavu. Systém automaticky zohledňuje tepelnou úpravu (`state='cooked'`):
- **Maso a ryby**: ztráta vody (úsušek ~20–25 %), přepočet koeficientem 1.20–1.30 na ekvivalent syrového masa.
- **Obiloviny, rýže, těstoviny a luštěniny**: absorpce vody (2.3–2.5× nárůst hmotnosti), přepočet koeficientem 0.35–0.43 na ekvivalent suché suroviny.
- **Brambory, vejce, ovoce**: výchozí poměr 1.00.

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
