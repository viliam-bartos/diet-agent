# Nutrition Agent

A minimalist diet, macronutrient, body weight, and workout tracking system operated through chat (CLI, Discord bot) and a companion web dashboard.

Google Gemini Flash (via the official `google-genai` SDK) functions strictly as a stateless natural language parser, converting unstructured text into validated Pydantic models. All nutritional calculations, DIAAS protein bioavailability discounting, dynamic caloric targets, and database operations execute deterministically in local Python against an SQLite database.

---

## Engineering Highlights & Reliability

- **LLM Hallucination Guardrails**: The LLM never calculates calories or guesses amino acid bioavailability freely. Instead, it categorizes protein sources into predefined biological domains (`animal`, `legumes`, `grains_seeds`, etc.) and returns raw nutritional claims. Local Python code calculates usable protein via DIAAS coefficients and verifies thermodynamic energy consistency ($4 \times \text{Protein} + 4 \times \text{Carbs} + 9 \times \text{Fats} + 7 \times \text{Alcohol}$). Any model estimate with $>35\%$ energy deviation is rejected.
- **Two-Tier Catalog & API Call Reduction**: Incoming food entries pass through local diacritic-tolerant fuzzy matching (`rapidfuzz`). The external LLM is invoked only when similarity falls below 75%. Once generated, verified estimates are cached in SQLite with an explicit `[AI Estimate]` origin tag, reusing the values in subsequent logs and eliminating redundant API costs without mutating curated ground truth.
- **Async Concurrency & Version Integrity**: The FastAPI server and Discord bot run concurrently in a single `asyncio` event loop over a shared SQLite database. The system is protected by 300+ automated tests (`pytest`), strict linting (`ruff`), and an API level handshake (`API_LEVEL`) ensuring the single-page dashboard and backend stay in lockstep.
- **Robust Domain Logic**: Built-in thermodynamic handling for raw-to-cooked yield factors (water evaporation in meats vs. hydration expansion in grains/rice), independent alcohol energy tracking (7 kcal/g non-macronutrient calculation), and dynamic weight-scaled nutritional targets.

---

## Architecture Overview

```
src/nutrition_agent/
├── bot.py             # Discord bot with slash commands, button pagination & DM logging
├── cli.py             # CLI parser, interactive shell & unified multi-process launcher
├── config.py          # Environment settings & Pydantic application configuration
├── db.py              # SQLite connection management & schema migrations
├── llm.py             # Stateless Gemini Flash parser with structured JSON output
├── models.py          # Strict Pydantic domain models, schemas & custom exceptions
├── nutrition.py       # DIAAS discounting, cooking state conversion & fuzzy matching
├── reporting.py       # Multi-period aggregations (Day, Week, Month, Half-Year, Year, All)
├── repository.py      # SQLite repository queries, moving averages & calendar heatmaps
├── targets.py         # Dynamic macro goals derived from current weight & day type
├── templates/         # Single-page vanilla HTML/CSS dashboard with Chart.js
└── web.py             # FastAPI REST endpoints & static dashboard server
```

- `data/foods.json`: Curated ground-truth food database seeded into SQLite upon first launch.
- `docs/USAGE.md`: Detailed operations guide, CLI flag reference, and Discord bot configuration.
- `spec.md` & `vault.md`: Technical domain specifications, DIAAS tables, and conversion benchmarks.

---

## Core Calculations & Nutritional Science

### 1. DIAAS Protein Bioavailability
Label protein does not equate to absorbable amino acids. The engine discounts gross protein using Digestible Indispensable Amino Acid Scores (DIAAS):

$$\text{Protein}_{\text{usable}} = \text{Protein}_{\text{raw}} \times \text{DIAAS Factor}$$

| Source Category | DIAAS Factor | Examples |
| :--- | :---: | :--- |
| Animal Protein | **1.00** | Chicken, beef, salmon, eggs, whey isolate, curd cheese |
| Plant Isolates & Tofu | **0.85** | Tofu, soy isolate, pea protein isolate |
| Legumes | **0.75** | Lentils, chickpeas, beans |
| Grains & Seeds | **0.70** | Oats, jasmine rice, semolina pasta |
| Nuts & Peanuts | **0.65** | Peanut butter, almonds, walnuts |
| Default / Unclassified | **0.70** | Conservative baseline for mixed meals |

### 2. Dynamic Weight-Scaled Targets
Rather than static calorie values, daily targets dynamically adjust relative to the user's latest logged body weight:

$$\text{Target}_{\text{kcal}} = \text{Weight}_{\text{kg}} \times \text{Ratio}_{\text{state, day}}$$

| Nutritional State | Day Type | kcal / kg | Protein (DIAAS) / kg | Carbs / kg | Fats / kg |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **Lean Bulk** | Training | 40.36 | 1.87 g | 5.18 g | 1.20 g |
| **Lean Bulk** | Rest | 34.34 | 1.75 g | 4.10 g | 1.20 g |
| **Maintenance** | Training | 33.13 | 1.81 g | 3.86 g | 0.99 g |
| **Maintenance** | Rest | 29.82 | 1.75 g | 3.73 g | 0.96 g |
| **Cut** | Training | 26.51 | 2.11 g | 2.59 g | 0.82 g |
| **Cut** | Rest | 25.90 | 2.11 g | 2.47 g | 0.78 g |

*Day types are tracked per date (workouts automatically designate a date as `training`; days without workouts default to `rest` unless explicitly tagged).*

### 3. Cooking State Yield Adjustments
The catalog stores values per 100 g in raw/dry state. When logging cooked items, water change coefficients apply:
- **Meats & Fish**: $\sim 20-25\%$ water reduction (raw weight factor: $1.20 - 1.30\times$).
- **Grains, Rice & Legumes**: $2.3 - 2.5\times$ hydration weight gain (dry weight factor: $0.35 - 0.43\times$).

---

## Quick Start

### Prerequisites
- Python $\ge 3.12$
- Google Gemini API Key (`GEMINI_API_KEY`)
- *(Optional)* Discord Bot Token (`DISCORD_BOT_TOKEN`)

### Local Setup
```bash
# 1. Clone repository
git clone https://github.com/viliam-bartos/diet-agent.git
cd diet-agent

# 2. Setup virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\Activate.ps1
# Linux/macOS:
source .venv/bin/activate

# 3. Install package with development dependencies
pip install -e ".[dev]"

# 4. Configure environment
cp .env.example .env
```

Fill in your API credentials in `.env`:
```env
GEMINI_API_KEY=your-gemini-api-key
DISCORD_BOT_TOKEN=your-discord-token
DISCORD_GUILD_ID=your-server-id
DATABASE_PATH=data/nutrition.db
FOODS_DB_PATH=data/foods.json
LOG_LEVEL=INFO
WEB_HOST=127.0.0.1
WEB_PORT=8000
DASHBOARD_URL=http://127.0.0.1:8000
```

### Running the System
```bash
# Launch interactive CLI chat
nutrition-agent

# Launch both Web Dashboard and Discord Bot concurrently
nutrition-agent --run-all --open-browser
```
On Windows, you can simply double-click `start.bat`.

---

## Docker Deployment

Run the system as an unattended service with automatic container restart:

```bash
docker compose up -d --build
docker compose logs -f
```

### Key Deployment Notes
- **Persistent Data**: The volume mount `./data:/app/data` preserves `nutrition.db` and learned food definitions across rebuilds.
- **Local Network & Tailscale Access**: The dashboard has no authentication layer. To access it across your private network or via Tailscale, set `WEB_BIND=<TAILSCALE_IP>` (e.g. `100.x.y.z`) and set `DASHBOARD_URL` to match so Discord links route to the accessible host.
- **Time Zone Sync**: SQLite evaluates day boundaries using local time; set `TZ` (default `Europe/Prague`) to match your local timezone.
- **CI/CD via Self-Hosted Runner**: A deployment workflow is included in `.github/workflows/deploy.yml`. When hosting on a public repository, ensure GitHub Actions runner permissions require administrator approval for outside pull requests.

---

## Interfaces & Features

### 1. Web Dashboard (`http://localhost:8000`)
- **Daily Macro KPI Cards**: Real-time progress bars for calories, DIAAS protein, carbs, and fats against dynamic targets.
- **7-Day Closed Week Balance**: Rolling balance of the last 7 completed days against caloric targets to measure net surplus/deficit without early-morning skew.
- **Interactive Weight Trend**: Annual weight chart (Chart.js) with 7-day moving average and dynamic boundary autoscaling.
- **Annual Adherence Heatmap**: 365-day calendar visualization displaying caloric and protein compliance plus workout indicators.
- **Interactive Food Log Editing**: In-place meal editing with macro values per 100 g and automated energy recomputation ($4\text{P} + 4\text{C} + 9\text{F} + 7\text{A}$).

### 2. Discord Bot
The bot supports natural language chat in direct messages or dedicated channels, plus slash commands:

| Command | Functionality |
| :--- | :--- |
| `/dnes` | Mobile overview: weight delta, workout status, Unicode macro progress bars, today's meal timeline. |
| `/vaha <value>` | Logs current weight with input validation and instant macro target recalibration. |
| `/trenink [type] [minutes] [note]` | Logs workout session and designates today as a `TRAINING` day. |
| `/prehled <period>` | Formatted tabular report for `tyden`, `mesic`, `pulrok`, `rok`, or `vse`. |
| `/vymaz [date]` | Interactive deletion of records for `dnes`, `vcera`, or `YYYY-MM-DD` with confirmation. |
| `/web` | Generates a direct access link to the web dashboard. |
| `/help` | Lists available commands with usage instructions. |

---

## Documentation & Manuals

For comprehensive command-line reference, manual food catalog editing, household unit overrides, and developer setup, refer to:
👉 [**docs/USAGE.md**](docs/USAGE.md)

---

## Testing & Quality Assurance

The codebase maintains 100% test coverage over core business logic and API integrations:

```bash
# Run pytest test suite (304 tests)
pytest

# Code style and linter checks
ruff check .
ruff format --check .
```

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
