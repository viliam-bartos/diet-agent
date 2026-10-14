# Usage & Operations Guide

This guide covers detailed CLI commands, Discord bot setup, food catalog administration, and deployment operations for **Nutrition Agent**.

---

## 1. CLI Usage & Commands

### Interactive Mode

Start the interactive console chat:
```bash
python -m nutrition_agent.cli
# or if installed as an editable package:
nutrition-agent
```

#### Natural Language Logging Examples
- `200g kuřecích prsou a 150g rýže` (logs 200 g raw chicken breast and 150 g dry rice)
- `Váha 83.2 kg` (logs body weight and automatically recalculates caloric & macro targets)
- `2 vejce a 1 banán` (resolves household units to grams: egg = 55 g, banana = 120 g)
- `trenink nohy 60 min` (logs workout and designates the day as `TRAINING`)
- `dnes` or `prehled` (displays today's nutrition summary and remaining targets)
- `tyden`, `mesic`, `pulrok`, `rok`, `vse` (multi-period aggregated reports and progress charts)
- `cil bulk`, `cil maintenance`, `cil cut` (switches active nutritional state)
- `den trenink`, `den odpocinek` (manually sets day type for the target date)
- `historie` (displays latest 10 logged records across food, weight, and workouts)
- `vymaz` (deletes today's records), `vymaz vcera` (yesterday), or `vymaz YYYY-MM-DD`
- `konec`, `exit`, `quit` (exits interactive session)

---

### One-Shot Commands & Command-Line Flags

#### Direct Message Logging
Pass your log directly as a single argument:
```bash
nutrition-agent "150g hovězího a 200g brambor"
```

#### Periodic Reports
```bash
nutrition-agent --today       # Today's macro adherence and remaining targets
nutrition-agent --week        # 7-day closed week summary (ends yesterday)
nutrition-agent --month       # Rolling 30-day summary
nutrition-agent --half-year   # Rolling 180-day summary
nutrition-agent --year        # Rolling 365-day summary
nutrition-agent --all         # Complete historical statistics
```

#### Profile and Day-Type Configuration
```bash
nutrition-agent --goal lean_bulk    # Options: lean_bulk, maintenance, cut
nutrition-agent --day-type training # Options: training, rest
```

#### History Inspection
```bash
nutrition-agent --history 10        # Inspect the last N logged records
```

#### Deleting Daily Records
```bash
nutrition-agent --delete-day             # Delete all records for today
nutrition-agent --delete-day vcera       # Delete all records for yesterday
nutrition-agent --delete-day 2026-09-15  # Delete all records for a specific date
```
*Note: Deleting a day removes logged meals, weight logs, and workouts for that day, but preserves user profile settings and catalog definitions.*

---

## 2. Food Catalog & Unit Conversions

The SQLite catalog stores nutritional data in two isolated tiers:
1. `curated`: Ground-truth values initialized from `data/foods.json`.
2. `ai`: Learned estimates generated when fuzzy matching is below 75% similarity.

### Managing Foods via CLI
```bash
jidlo kuřecí kari                    # View current macros per 100g
jidlo kuřecí kari 145 13 7 7         # Override: calories, protein, carbs, fats
jidlo pivo 44 0.5 3.6 0 4.4          # Optional 5th parameter for alcohol in grams
jidla                                # List all foods in catalog
jidla ai                             # List only AI-estimated foods
jidla curated                        # List only curated foods
jidla smaz máslo                     # Remove food from catalog
```

### Household Unit Conversions
The system converts units in the following resolution order:
1. **Absolute metrics**: `g`, `ml` are used directly.
2. **Curated table**: Fixed weights for common household measures (e.g., `lžíce` = 13 g, `lžička` = 5 g, `odměrka` = 30 g, `vejce` = 55 g, `banán` = 120 g).
3. **Learned conversions (`unit_conversions` table)**: Persisted historical conversions.
4. **LLM estimate**: If unknown, the model estimates the unit weight; the estimate is saved in SQLite for future queries.

```bash
prevod                               # List learned unit conversions
prevod svíčková porce 350            # Manually map 1 portion of 'svíčková' to 350g
```

---

## 3. Discord Bot Setup & Operations

### Step-by-Step Discord Setup
1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create a New Application.
2. Under the **Bot** tab:
   - Click **Reset Token** to generate a bot token. Copy this to `DISCORD_BOT_TOKEN` in your `.env`.
   - Scroll to **Privileged Gateway Intents** and enable **Message Content Intent** (required to read meal logs from chat).
3. Under **OAuth2 → URL Generator**:
   - Check scopes: `bot`, `applications.commands`.
   - Permissions: `Send Messages`, `Embed Links`, `Read Message History`.
   - Open the generated invite link in your browser to authorize the bot on your server.
4. *(Recommended)* Set `DISCORD_GUILD_ID` in `.env` to your server ID:
   - Enable Developer Mode in Discord (`User Settings → Advanced → Developer Mode`).
   - Right-click your server icon and click **Copy Server ID**.
   - Setting this ID registers slash commands instantly for your server instead of waiting up to an hour for Discord's global sync.

### Running Services
```bash
nutrition-agent --web                    # Web dashboard only (WEB_HOST:WEB_PORT)
nutrition-agent --bot                    # Discord bot only
nutrition-agent --run-all                # Both concurrently in single asyncio loop
nutrition-agent --run-all --open-browser # Concurrently and automatically open browser
```

---

## 4. Windows Quick Launcher (`start.bat`)

The included `start.bat` provides one-click startup on Windows:
- Validates `.venv` and `.env` presence.
- Automatically kills any leftover processes holding port 8000 from previous sessions to prevent port collisions.
- Launches the web service and Discord bot in a single terminal.
- Opens `http://127.0.0.1:8000` in the default browser.
