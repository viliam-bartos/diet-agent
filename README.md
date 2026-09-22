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
- `data/foods.json`: Kurátorovaný katalog potravin, který se při startu dosévá do tabulky `foods`.
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

Skript před spuštěním **ukončí předchozí instanci**, která by jinak držela port. Bez toho by nový server nedokázal obsadit port, spadl by a prohlížeč by dál mluvil se starým procesem — navenek by to vypadalo, že aplikace ignoruje nový kód.

Ekvivalentní příkaz pro ruční spuštění je `nutrition-agent --run-all --open-browser`.

Když dashboard zobrazí červený pruh „Server běží se starou verzí“, znamená to, že běžící proces je starší než načtená stránka. Zavři jeho okno a spusť `start.bat` znovu.

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
- `jidlo kuřecí kari` pro zobrazení nebo opravu hodnot potraviny v katalogu
- `jidla ai` pro výpis naučených potravin, `jidla smaz <nazev>` pro jejich smazání
- `prevod svíčková porce 350` pro opravu hmotnosti kuchyňské míry

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
| `GET /api/version` | Verze běžícího serveru. |
| `GET /api/day?date=` | Jeden den: spotřeba, cíl, zbývající hodnoty a seznam jídel. Bez data znamená dnešek. |
| `GET /api/week?date=` | Posledních 7 dní: součty, bilance proti cílům a rozpis po dnech. |
| `GET /api/weight-trend?days=` | Body jednotlivých vážení, 7denní klouzavý průměr a souhrnné hodnoty pro Chart.js. Výchozích 365 dní. |
| `GET /api/heatmap?days=` | Adherence kalendář po dnech. Výchozích 365 dní. |
| `GET /api/records?type=&limit=&offset=` | Stránkovaná a filtrovaná historie (jídla, váha, trénink). |
| `PATCH /api/records/{typ}/{id}` | Úprava záznamu. U jídla se přepočítají makra z nové gramáže. |
| `DELETE /api/records/{typ}/{id}` | Smazání jednoho záznamu. |
| `POST /api/message` | Zpracuje textovou zprávu stejnou pipeline jako CLI. Nepovinné `date` zapíše záznam na zvolený den. |

### Komponenty dashboardu

- **Karty maker**: dnešní příjem kalorií, bílkovin, sacharidů a tuků proti dnešnímu cíli, s procentem plnění, pruhem a tím, kolik ještě zbývá. Každý den začínají od nuly.
- **Bilance týdne**: součet příjmu za posledních 7 dní proti součtu cílů pro ty dny. Kladné číslo znamená přebytek, záporné deficit. Pod tím je rozpis po dnech, na který se dá kliknout.
- **Interaktivní graf váhy** (Chart.js): celý rok, osa Y se škáluje podle dat. Body jednotlivých vážení a spojitá čára 7denního klouzavého průměru.
- **Adherence kalendář**: celý rok najednou, s popisky měsíců a dny v týdnu. Zelená = splněné kalorie i bílkoviny, modrá = splněné bílkoviny, oranžová = záznam pod cílem, bílý bod = tréninkový den. Dnešní den má oranžový rámeček.
- **Panel dne**: kliknutím na den v kalendáři nebo v týdenním rozpisu se zobrazí jeho jídla a součty. Nahoře v poli pro záznam se pak píše na vybraný den, takže se dají jídla doplnit zpětně. Tlačítkem se výběr zruší.
- **Tabulka záznamů**: stránkování, filtrování podle typu a tlačítka pro úpravu a smazání záznamu. Úprava jídla mění jen gramáž a stav, makra a kalorie se přepočítají z hodnot potraviny, aby čísla zůstala konzistentní.

Dashboard používá barevnou paletu z `--bg-paper`, `--valve-orange` a `--aperture-cyan` s fonty Barlow Semi Condensed a Space Mono.

### Hranice dne

Časové značky se ukládají v UTC, ale „den" znamená tvůj místní den. Dotazy na den proto filtrují přes `date(logged_at, 'localtime')`. Bez toho by jídlo zapsané mezi půlnocí a druhou hodinou ranní spadlo do předchozího dne.

### Cíl pro jednotlivé dny

Kalendář a týdenní bilance neporovnávají všechny dny proti jednomu cíli:

- **Dnešek** se měří proti cíli podle aktivního nastavení (`den trenink` / `den odpocinek`), protože trénink ještě může přijít.
- **Minulé dny** se měří podle toho, jestli v nich je zapsaný trénink. Den s tréninkem má vyšší cíl, den bez něj nižší. Odpočinkový den tak není hodnocen jako pod cílem jen proto, že tréninkový cíl je výš.


---

## Katalog potravin a automatické doplňování

Katalog žije v tabulce `foods` v SQLite a má dvě vrstvy, které se nikdy nemíchají:

| Vrstva | Zdroj | Význam |
| :--- | :--- | :--- |
| `curated` | `data/foods.json` | Kurátorované hodnoty, které jsi zadal nebo ověřil. |
| `ai` | Odhad modelu | Přibližné hodnoty pro potraviny, které v katalogu chyběly. |

**Lokální katalog má vždy přednost.** Model se na hodnoty ptá jen ve chvíli, kdy fuzzy párování selže (skóre pod 75 %). Když potravinu odhadne, rovnou ji uloží, takže při příštím záznamu se použijí stejná čísla a model se nevolá znovu.

### Jak se odhad pozná

- V odpovědi je u položky `[AI odhad maker]` a pod ní výpis uložených hodnot.
- V `food_logs` je uložen sloupec `nutrition_source`, takže příznak přežije i do historie a dashboardu (řádek má v názvu `[AI odhad]`).
- V souhrnech se odhad neschovává za měření — je vidět, odkud čísla jsou.

### Oprava hodnot

Odhad jde kdykoliv přepsat na reálné hodnoty z etikety:

```
jidlo kuřecí kari                    # zobrazí aktuální hodnoty na 100 g
jidlo kuřecí kari 145 13 7 7         # přepíše kcal, bílkoviny, sacharidy, tuky
```

Hodnoty jsou vždy na 100 g, oddělovač může být i desetinná čárka. Oprava se vztahuje na budoucí záznamy; už zapsané dny se nepřepočítávají, aby historie zůstala konzistentní.

### Správa katalogu

```
jidla                  # celý katalog
jidla ai               # jen potraviny naučené odhadem
jidla curated          # jen kurátorované potraviny
jidla smaz máslo       # odebere potravinu z katalogu
```

Smazáním potraviny se nemažou už zapsané dny, jen se přestane nabízet pro nové záznamy.

### Kuchyňské míry a jejich převod na gramy

Parser zachová míru, kterou jsi napsal, a na gramy ji převádí až lokální kód. Model tedy nikdy neurčuje, kolik gramů má lžíce — jen rozpozná, že jsi lžíci použil.

Převod se hledá v tomto pořadí:

1. **Absolutní jednotky** — `g` a `ml` platí beze změny.
2. **Kurátorovaná tabulka** — `lžíce` 13 g, `lžička` 5 g, `hrnek` 240 g, `odměrka` 30 g, `plátek` 20 g, `krajíc` 40 g. Kusové potraviny mají vlastní váhu: vejce 55 g, banán 120 g, jablko 150 g.
3. **Naučené převody** — co se jednou odhadlo nebo zadal, se ukládá do tabulky `unit_conversions` a příště se použije stejná hodnota.
4. **Odhad modelu** — teprve když nic z výše uvedeného neexistuje, zeptá se model. Výsledek se uloží a záznam se označí `[odhad převodu]`.

Když model jednotku nezná, řekne to a záznam se odmítne s výzvou zadat gramy. Vymyšlená hodnota se neuloží.

```
prevod                                   # výpis naučených převodů
prevod svíčková porce 350                # ruční oprava na 350 g za porci
```

Dřív kód u neznámé kusové potraviny tiše počítal 100 g a u porce 150 g. To vypadalo jako měření, ale byl to vymyšlený údaj — teď se místo toho počká na odhad nebo na tebe.

### Odhad množství bez váhy

Když neuvedeš gramy, agent si množství odhadne sám. Nikdy kvůli tomu záznam neodmítne:

```
měl jsem rýži                     ->  1 porce vařené rýže (220 g)
měl jsem chicken butter           ->  1 porce (350 g)
dal jsem si pivo                  ->  500 ml
vypil jsem dvě piva               ->  1000 ml
panák rumu                        ->  40 ml
měl jsem hrst ořechů              ->  1 ks (odhad váhy kusu)
```

U nápojů se odhaduje objem v mililitrech, protože teprve ten se dá přepočítat na kalorie. U hotových jídel porce, u syrových surovin také porce.

Do odhadu hmotnosti jednotky jde i **stav potraviny**, protože porce suché rýže je kolem 80 g, kdežto porce vařené kolem 220 g. Bez toho by se úsušek uplatnil dvakrát a vyšlo by z toho třetinové číslo.

### Alkohol

Etanol má 7 kcal/g a **není to makroživina**, takže se ukládá zvlášť ve sloupci `alcohol`. Bez toho by pivo nešlo zapsat: jeho energie pochází z alkoholu, ne ze sacharidů, a kontrola konzistence by odhad odmítla jako nesmyslný.

```
dal jsem si pivo      ->  pivo (500 g): 215.0 kcal | ... | Alkohol: 19.0 g [AI odhad maker]
vypil jsem dvě piva   ->  pivo (1000 g): 430.0 kcal | ... | Alkohol: 38.0 g
```

Energetická bilance se počítá jako `bílkoviny*4 + sacharidy*4 + tuky*9 + alkohol*7` a platí pro odhad stejně jako pro ruční opravu.

Ruční oprava umí alkohol nastavit jako pátou hodnotu. Když ji vynecháš, uložená hodnota zůstane — aby oprava piva omylem nesmazala jeho alkoholové kalorie:

```
jidlo pivo                       # zobrazí i alkohol, pokud nějaký je
jidlo pivo 44 0.5 3.6 0          # alkohol zůstane
jidlo pivo 44 0.5 3.6 0 4.4      # alkohol se přepíše na 4.4 g
```

### Co model neumí

- **DIAAS koeficient** se neodhaduje volně. Model vybere jednu z pěti kategorií bílkovin (`animal`, `plant_isolate`, `legumes`, `grains_seeds`, `nuts_peanuts`) a koeficient se dopočítá lokálně. Když si není jistý, použije se konzervativní `0.70`.
- **Nekonzistentní odhad se odmítne.** Pokud makra a alkohol neodpovídají uvedené energii (odchylka nad 35 %), hodnoty se neuloží.
- **Neznámou potravinu model přizná.** Když vrátí samé nuly, záznam se odmítne s chybou místo vymyšlených čísel.

Tyto hodnoty jsou odhad, ne měření. Přesnost je u základních surovin vysoká, u složených jídel klidně ±30 %.

### Kdy se model vůbec nevolá

Párování je tolerantní, aby se odhad zbytečně nespouštěl u potravin, které v katalogu jsou:

- **Kratší název než v katalogu.** `máslo` se spáruje s `Máslo 82%`, `cfm protein` s `Prom-IN CFM Pure Performance (syrovátkový protein)`. Stačí, když jsou všechna slova z dotazu obsažena v názvu potraviny.
- **Bez diakritiky.** `maslo`, `sunka`, `ryze`, `kureci prsa` se přeloží na správné položky, protože se diakritika při porovnávání slov skládá. Týká se to i slovníku synonym.
- **Kurátorovaná položka má vždy přednost.** Když je přijatelná shoda v katalogu i mezi odhady, vybere se katalogová. Odhad tedy nikdy nezastíní ověřenou potravinu.

Když se i tak spustí odhad, znamená to, že v katalogu opravdu není nic použitelného.

---

## Databázové schéma (SQLite)

| Tabulka | Účel |
| :--- | :--- |
| `food_logs` | Záznamy jídel s makry, DIAAS bílkovinami a zdrojem hodnot (`nutrition_source`). |
| `weight_logs` | Záznamy tělesné hmotnosti. |
| `workout_logs` | Záznamy tréninků (typ, délka, poznámka). |
| `foods` | Katalog potravin, kurátorovaný i odhadnutý, s vyhledávacím klíčem `name_key`. |
| `unit_conversions` | Naučené převody kuchyňských měr na gramy pro konkrétní potraviny. |
| `user_settings` | Aktivní cíl, typ dne a referenční hodnoty profilu. |

Sloupec `name_key` drží název převedený na malá písmena přes Python, protože SQLite vestavěná kolace `NOCASE` skládá jen ASCII a `KUŘECÍ` by se s `kuřecí` neshodovalo.

Schéma se inicializuje idempotentně při startu CLI, webu i bota. Databáze z starší verze se automaticky doplní o chybějící sloupce a tabulky; kurátorované potraviny se dosévají přes `INSERT ... ON CONFLICT DO NOTHING`, takže tvoje opravy ani naučené potraviny se nikdy nepřepíšou.

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

### Odkud se cíl bere

Cíl není pevné číslo. Počítá se jako **aktuální váha krát poměr na kilogram**:

```
cíl_kcal = váha × kcal_na_kg
```

Poměry jsou kalibrované na 83 kg a liší se podle stavu a typu dne:

| Stav | Typ dne | kcal/kg | Bílkoviny/kg | Sacharidy/kg | Tuky/kg |
| :--- | :--- | ---: | ---: | ---: | ---: |
| Lean Bulk | trénink | 37.95 | 1.87 | 5.18 | 0.99 |
| Lean Bulk | odpočinek | 31.93 | 1.75 | 4.10 | 0.93 |
| Maintenance | trénink | 33.13 | 1.81 | 3.86 | 0.99 |
| Maintenance | odpočinek | 29.82 | 1.75 | 3.73 | 0.96 |
| Cut | trénink | 26.51 | 2.11 | 2.59 | 0.82 |
| Cut | odpočinek | 25.90 | 2.11 | 2.47 | 0.78 |

Váha pro výpočet se bere z **posledního záznamu vážení**, nezávisle na zvoleném období. Když žádné vážení neexistuje, použije se hodnota z `user_settings`.

Cíl se tedy sám mění, když:

- zaznamenáš novou váhu (CLI, Discord `/vaha`, nebo zprávou),
- přepneš stav přes `cil bulk|maintenance|cut`,
- přepneš typ dne přes `den trenink|odpocinek`.

Dvě věci stojí za pozornost:

- **Cíl je vždy dnešní.** Dashboard porovnává i staré záznamy proti současnému cíli, protože historické cíle se neukládají. Při změně váhy se tedy zpětně změní i procenta plnění u minulých měsíců.
- **`TARGET_CALORIES` a `TARGET_PROTEIN_G` v `.env` přebíjí výpočet.** Když je nastavíš, zůstanou pevné a nebudou se s váhou měnit.

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
