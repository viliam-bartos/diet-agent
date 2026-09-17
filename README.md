# Nutrition Agent

Minimalistický systém pro záznam stravy, maker, tělesné hmotnosti a tréninků formou chatu (CLI, Discord bot) s webovým dashboardem.

Model Google Gemini Flash (přes oficiální SDK `google-genai`) slouží výhradně jako bezstavový parser pro extrakci entit z volného textu do striktního Pydantic schématu. Veškeré výpočty, kalorie a databázové operace probíhají deterministicky v lokálním kódu v Pythonu s diskontováním bílkovin podle DIAAS a ukládáním do lokální SQLite databáze.

---

## Architektura projektu

- `src/nutrition_agent/models.py`: Pydantic schémata (`UserLog`, `ExtractedFoodItem`, `FoodDefinition`, `CalculatedFoodLog`, `CalculatedWeightLog`, `CalculatedWorkoutLog`) a doménové výjimky (`FoodNotFoundError`, `WeightValidationError`).
- `src/nutrition_agent/nutrition.py`: Výpočet využitelných bílkovin (DIAAS), přepočet jednotek na gramy, přepočet tepelné úpravy, validace tělesné hmotnosti a fuzzy párování potravin (`rapidfuzz`).
- `src/nutrition_agent/targets.py`: Nutriční stavy (Lean Bulk, Maintenance, Cut), typy dne a dynamický výpočet cílů podle váhy.
- `src/nutrition_agent/llm.py`: Bezstavový parser volající Google Gemini Flash přes `google-genai` SDK se strukturovaným JSON výstupem.
- `src/nutrition_agent/db.py`: SQLite inicializace schématu a správa spojení.
- `src/nutrition_agent/repository.py`: Veškeré DB dotazy a agregace (denní okna, delta váhy, 7denní klouzavý průměr, heatmapa, stránkování).
- `src/nutrition_agent/reporting.py`: Textové přehledy a grafy pro denní, týdenní, měsíční, půlroční, roční a kompletní období.
- `src/nutrition_agent/config.py`: Konfigurace aplikace a načítání proměnných prostředí.
- `src/nutrition_agent/cli.py`: Rozhraní příkazové řádky pro interaktivní režim, jednorázové zprávy a spouštění služeb.
- `start.bat`: Spouštěč pro Windows — dvojklikem nastartuje web i bota a otevře dashboard.
- `src/nutrition_agent/bot.py`: Discord bot se Slash Commands a nasloucháním na zprávy.
- `src/nutrition_agent/web.py`: FastAPI server a REST API dashboardu.
- `src/nutrition_agent/templates/dashboard.html`: HTML šablona dashboardu s čistým CSS a Chart.js.
- `data/foods.json`: Lokální katalog potravin s nutričními hodnotami a DIAAS koeficienty.
- `spec.md`: Technická specifikace datového toku, SQLite schématu a DIAAS koeficientů.
- `vault.md`: Referenční tabulka surovin, převodní jednotky a pravidla pro fuzzy matching.

---

## Požadavky

- Python >= 3.12
- API klíč pro Google Gemini (`GEMINI_API_KEY`)
- Token Discord bota (`DISCORD_BOT_TOKEN`) — pouze pro Discord bota

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
   Do souboru `.env` doplň klíče a volitelné cíle:
   ```env
   GEMINI_API_KEY=tvuj-gemini-api-klic
   DISCORD_BOT_TOKEN=tvuj-discord-token
   DISCORD_GUILD_ID=id-tveho-serveru
   DATABASE_PATH=data/nutrition.db
   FOODS_DB_PATH=data/foods.json
   LOG_LEVEL=INFO
   WEB_HOST=127.0.0.1
   WEB_PORT=8000
   TARGET_CALORIES=3150
   TARGET_PROTEIN_G=155
   ```

   `TARGET_CALORIES` a `TARGET_PROTEIN_G` jsou volitelné. Pokud nejsou nastavené, dashboard použije cíle odvozené z aktuální váhy, aktivního stavu a typu dne.

   `DISCORD_GUILD_ID` je volitelné, ale doporučené — slash příkazy se pak v Discordu zaregistrují okamžitě místo až hodinového čekání.

---

## Rychlý start na Windows

Po instalaci stačí dvojkliknout na **`start.bat`** v kořeni projektu. Skript:

1. ověří, že existuje `.venv` a `.env` a že jsou doinstalované závislosti,
2. spustí webový dashboard i Discord bota v jednom okně,
3. otevře dashboard v prohlížeči.

Ukončení: `Ctrl+C` v okně, případně potvrď `Y`. Při chybějící konfiguraci skript vypíše, který příkaz máš spustit, a okno nechá otevřené.

Ekvivalentní příkaz pro ruční spuštění je `nutrition-agent --run-all --open-browser`.

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
- `trenink nohy 60 min` (zaznamená trénink a přepne den na TRAINING)
- `dnes` nebo `prehled` pro zobrazení denního souhrnu, plnění cílů a zbývajících hodnot
- `tyden`, `mesic`, `pulrok`, `rok`, `vse` pro agregované přehledy s grafy
- `cil bulk`, `cil maintenance`, `cil cut` pro změnu aktivního stavu
- `den trenink` nebo `den odpocinek` pro přepnutí tréninkového stavu
- `historie` pro zobrazení posledních záznamů
- `vymaz` pro smazání dnešních záznamů, `vymaz vcera` nebo `vymaz 2026-09-15` pro jiný den

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

Smazání všech záznamů za den:
```bash
nutrition-agent --delete-day             # Dnešní den
nutrition-agent --delete-day vcera       # Včerejšek
nutrition-agent --delete-day 2026-09-15  # Konkrétní datum
```

Mazání se týká jídel, vážení i tréninků daného dne. Uživatelský profil (cíl, typ dne) zůstává nedotčený. Neplatné datum se odmítne a nesmaže se nic.

### Služby (web a Discord bot)

Webový dashboard i Discord bota lze spustit samostatně nebo souběžně:
```bash
nutrition-agent --web                  # Pouze webový dashboard na WEB_HOST:WEB_PORT
nutrition-agent --bot                  # Pouze Discord bot
nutrition-agent --run-all              # Obojí souběžně v jednom asyncio event loopu
nutrition-agent --run-all --open-browser  # Navíc otevře dashboard v prohlížeči
```

Přepínač `--open-browser` funguje i s `--web`. Pokud v `.env` chybí `DISCORD_BOT_TOKEN`, režim `--run-all` bota přeskočí s upozorněním a dashboard poběží dál. Stejně tak pád bota během běhu nezastaví webový dashboard.

Alternativně lze služby spustit jako moduly:
```bash
python -m nutrition_agent.web
python -m nutrition_agent.bot
```

---

## Discord bot

Bot kombinuje přímé naslouchání na zprávy s moderními Slash Commands. Veškeré DB dotazy a agregace řeší `repository.py`; bot pouze skládá odpovědi.

### Zprovoznění bota

1. Vytvoř aplikaci na https://discord.com/developers/applications a v sekci **Bot** vygeneruj token. Ten vlož do `.env` jako `DISCORD_BOT_TOKEN`.
2. V sekci **Bot → Privileged Gateway Intents** zapni **Message Content Intent**. Bez něj se bot nepřihlásí, protože potřebuje číst text zpráv.
3. Pozvi bota na server. Spuštěním `nutrition-agent --bot` se vypíše pozvánkový odkaz (obsahuje scopes `bot` a `applications.commands`) — stačí ho otevřít v prohlížeči a vybrat server. Případně si odkaz vygeneruj ručně přes **OAuth2 → URL Generator**.
4. Volitelně nastav `DISCORD_GUILD_ID` na ID svého serveru (v Discordu zapni *Nastavení → Rozšířené → Vývojářský režim*, pak klikni pravým na server → *Kopírovat ID serveru*). Slash příkazy se pak zaregistrují okamžitě. Bez tohoto ID se registrují globálně, což může trvat až hodinu.

Bot umí odpovídat v přímých zprávách i ve zmíněném kanálu. Aby mohl odpovídat v kanálu, musí mít oprávnění *Posílat zprávy* a *Vkládat odkazy*.

### Přirozená konverzace

Zpráva v DM nebo ve vyhrazeném kanálu (např. `200g kuřecích prsou a 150g rýže`) projde stejnou pipeline jako CLI. Odpověď je Discord Embed:
- zelený pruh při úspěchu, červený při chybě,
- výpis položek s makry a DIAAS bílkovinami,
- tlačítko s odkazem na webový dashboard v patičce.

### Slash Commands

| Příkaz | Popis |
| :--- | :--- |
| `/dnes` | Mobilní přehled: hmotnost s deltou proti předchozímu vážení, status tréninku, makra s textovými progress bary a seznam dnešních jídel podle času. |
| `/vaha <hodnota>` | Okamžitý záznam hmotnosti s validací a automatickým přepočtem cílů. |
| `/trenink [typ] [delka_min] [poznamka]` | Rychlý záznam tréninku a přepnutí typu dne na TRAINING. |
| `/prehled <obdobi>` | Textová tabulka za období (`tyden`, `mesic`, `pulrok`, `rok`, `vse`). |
| `/vymaz [datum]` | Smaže všechny záznamy za den (`dnes`, `vcera` nebo `RRRR-MM-DD`). Před smazáním zobrazí, co se odstraní, a vyžádá potvrzení tlačítkem. |
| `/web` | Přímý odkaz na webový dashboard. |

Progress bary v Discordu používají Unicode bloky:
```text
Kalorie:   [████████░░░░] 65 %  (1625 / 2500 kcal)
Bílkoviny: [██████████░░] 82 %  (131 / 160 g)
```

---

## Webový dashboard

Server vystavuje HTML dashboard a REST API nad stejnou SQLite databází.

| Endpoint | Popis |
| :--- | :--- |
| `GET /` | HTML dashboard. |
| `GET /api/kpis?period=` | KPI karty: aktuální váha s deltou za období, průměrné kalorie a DIAAS bílkoviny, počet odtrénovaných dní. |
| `GET /api/weight-trend?period=` | Body jednotlivých vážení a 7denní klouzavý průměr pro Chart.js. |
| `GET /api/heatmap?period=` | Kalendářní adherence heatmapa po dnech. |
| `GET /api/records?type=&limit=&offset=` | Stránkovaná a filtrovaná historie (jídla, váha, trénink). |
| `POST /api/message` | Zpracuje textovou zprávu stejnou pipeline jako CLI a vrátí odpověď. |

Parametr `period` přijímá `7d`, `30d`, `90d`, `180d`, `365d` a `all`.

### Komponenty dashboardu

- **Přepínače časového rozsahu**: 7 dní, 30 dní, 3 měsíce, 6 měsíců, 1 rok, Vše.
- **KPI karty**: aktuální váha s deltou za zvolené období, průměrný denní příjem kalorií a využitelných bílkovin (DIAAS), celkový počet odtrénovaných dní.
- **Interaktivní graf váhy** (Chart.js): body jednotlivých vážení a spojitá čára 7denního klouzavého průměru.
- **Kalendářní heatmapa** ve stylu GitHubu: zelená = splněné kalorie i bílkoviny, modrá = splněné bílkoviny, oranžová = záznam pod cílem, bílý bod = tréninkový den.
- **Tabulka záznamů**: stránkování, filtrování podle typu a zobrazení původního textu záznamu.

Dashboard používá barevnou paletu z `--bg-paper`, `--valve-orange` a `--aperture-cyan` s fonty Barlow Semi Condensed a Space Mono.

---

## Databázové schéma (SQLite)

| Tabulka | Účel |
| :--- | :--- |
| `food_logs` | Záznamy jídel s makry a DIAAS bílkovinami. |
| `weight_logs` | Záznamy tělesné hmotnosti. |
| `workout_logs` | Záznamy tréninků (typ, délka, poznámka). |
| `user_settings` | Aktivní cíl, typ dne a referenční hodnoty profilu. |

Schéma se inicializuje idempotentně (`CREATE TABLE IF NOT EXISTS`) při startu CLI, webu i bota, takže databáze vytvořené starší verzí se automaticky doplní o chybějící tabulky.

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
