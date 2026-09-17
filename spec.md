# Technická specifikace

Systém pro záznam výživy a tělesné hmotnosti. Architektura odděluje sémantické parsování přirozeného jazyka (LLM) od deterministických matematických výpočtů a perzistence (lokální Python kód).

---

## 1. Architektura a datový tok

Systém zpracovává každý vstup izolovaně jako bezstavovou transakci:

1. **Vstupní zpráva**: Uživatel odešle textový řetězec přes CLI nebo chat (např. *"Dneska k obědu 200g kuřecích prsou a 150g rýže"* nebo *"Váha 82.4 kg"*).
2. **LLM Parser (`llm.py`)**:
   - Vstup je předán modelu Gemini Flash přes SDK `google-genai`.
   - Model vrací striktní strukturovaný JSON odpovídající Pydantic schématu `UserLog`.
   - Žádná nutriční data se v promptu nevypočítávají; LLM pouze extrahuje normalizovaný název suroviny, množství a měrnou jednotku.
3. **Validace a selhání (`models.py`)**:
   - Pydantic zvaliduje schéma.
   - Pokud je typ záznamu vyhodnocen jako `INVALID` nebo chybí povinná pole, pipeline okamžitě selže (Fail Fast) a vrátí uživateli chybovou hlášku.
4. **Nutriční engine (`nutrition.py`)**:
   - Extrahovaná surovina projde přes fuzzy matching vůči lokální databázi `data/foods.json`.
   - Vypočtou se kalorie, sacharidy, tuky a celkové bílkoviny.
   - Aplikuje se DIAAS diskontování na bílkoviny podle typu suroviny.
5. **Perzistence (`repository.py` / `db.py`)**:
   - Validní a dopočtená data se zapíší v transakci do lokální SQLite databáze.

---

## 2. Databázové schéma (SQLite)

Databáze ukládá normalizované hodnoty v základních jednotkách (gramy, kcal, kilogramy). Časové značky používají formát ISO 8601 UTC.

```sql
CREATE TABLE IF NOT EXISTS food_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    raw_input TEXT NOT NULL,
    food_name_matched TEXT NOT NULL,
    amount_g REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('raw', 'cooked')),
    is_estimated INTEGER NOT NULL CHECK (is_estimated IN (0, 1)),
    calories REAL NOT NULL,
    protein_raw REAL NOT NULL,
    protein_usable REAL NOT NULL,
    carbs REAL NOT NULL,
    fats REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_food_logs_logged_at ON food_logs(logged_at);
CREATE INDEX IF NOT EXISTS idx_food_logs_estimated ON food_logs(is_estimated);
```

---

## 3. Výpočet využitelných bílkovin (DIAAS logika)

Hrubý příjem bílkovin z etiket neodpovídá množství aminokyselin, které tělo skutečně zabuduje do tkání. Rostlinné proteiny mají nižší stravitelnost v tenkém střevě a horší aminokyselinové spektrum (limitující aminokyseliny jako lysin, methionin, threonin).

Výpočet provádí lokální kód podle vzorce:

$$\text{Protein}_{\text{usable}} = \text{Protein}_{\text{raw}} \times \text{Koeficient}$$

### Pravidla pro přiřazení koeficientů:

| Kategorie | Koeficient | Příklady surovin | Zdůvodnění |
| :--- | :--- | :--- | :--- |
| **Živočišné zdroje** | **1.00** | maso, vejce, mléčné výrobky, syrovátkový protein | Vysoké DIAAS skóre (> 100 %), kompletní spektrum esenciálních aminokyselin, téměř stoprocentní stravitelnost. |
| **Rostlinné izoláty a zpracované proteiny** | **0.85** | sójový izolát, tofu, hrachový izolát | Odstraněna většina antinutričních látek a vlákniny, stravitelnost je mírně snížena horším spektrem aminokyselin. |
| **Běžné luštěniny a celistvé rostlinné zdroje** | **0.75** | čočka, fazole, cizrna | Přítomnost fytátů, taninů a inhibitorů proteáz snižuje vstřebatelnost v ileu. |
| **Obiloviny a semena** | **0.70** | ovesné vločky, rýže, těstoviny, chléb | Výrazný deficit lysinu a horší stravitelnost matrixu zrna. |
| **Ořechy a arašídy** | **0.65** | vlašské ořechy, mandle, arašídy, arašídové máslo | Vysoký obsah vlákniny, buněčné stěny bránící přístupu enzymů, nevyvážený profil. |

> **Výchozí hodnota**: Pokud u suroviny není koeficient explicitně zadán v databázi potravin, použije se konzervativní výchozí hodnota **0.70**.

---

## 4. Ošetření chyb a hraničních stavů (Fail Fast)

Pro zajištění integrity dat a okamžité zpětné vazby uživateli systém striktně aplikuje princip Fail Fast:

1. **Vstup bez specifikovaného množství**:
   - **Kusové potraviny**: Pokud uživatel zadá kusovou potravinu bez čísla (např. *"Měl jsem jablko"*), parser nastaví `amount: 1`, `unit: "ks"` a `is_estimated: true`.
   - **Vážené / nespočitatelné suroviny**: Pokud uživatel napíše obecnou surovinu bez množství (např. *"Měl jsem rýži"*), parser vrátí `log_type: "INVALID"` s chybovou zprávou: `"Nespecifikované množství suroviny"`.
2. **Absurdní hodnoty (validace váhy)**:
   - Tělesná hmotnost musí spadat do fyziologického intervalu $30 \le \text{hmotnost} \le 300\text{ kg}$.
   - Pokud parser vrátí hodnotu mimo tento rozsah (pod 30 kg nebo nad 300 kg), lokální validátor v Pythonu vyvolá výjimku `ValueError` (či odvozenou `WeightValidationError`) ještě před jakýmkoliv pokusem o zápis do databáze.
3. **Nerozpoznaná surovina**:
   - Extrahovaná surovina je porovnána vůči databázi `data/foods.json` pomocí fuzzy matching metriky (`token_sort_ratio`).
   - Pokud nejvyšší dosažené skóre shody nedosáhne prahové hodnoty **75 %**, kód okamžitě přeruší zpracování vyvoláním výjimky `FoodNotFoundError` a vyzve uživatele k upřesnění nebo manuálnímu zadání nutričních hodnot.