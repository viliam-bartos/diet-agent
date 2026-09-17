# Znalostní báze a konfigurace (vault.md)

Referenční tabulka surovin, převodní koeficienty a pravidla pro fuzzy párování.

---

## 1. Referenční databáze potravin (hodnoty na 100g v syrovém stavu)

| Surovina | Energie (kcal) | Bílkoviny (g) | DIAAS koef. | Využitelné bílkoviny (g) | Sacharidy (g) | Tuky (g) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Kuřecí prsa | 106 | 23.0 | 1.00 | 23.0 | 0.0 | 1.2 |
| Hovězí zadní | 133 | 21.0 | 1.00 | 21.0 | 0.0 | 5.5 |
| Losos | 208 | 20.0 | 1.00 | 20.0 | 0.0 | 13.0 |
| Vejce (celé) | 143 | 12.6 | 1.00 | 12.6 | 0.7 | 9.9 |
| Tvaroh odtučněný | 68 | 12.0 | 1.00 | 12.0 | 4.0 | 0.2 |
| Syrovátkový protein (80%) | 390 | 80.0 | 1.00 | 80.0 | 6.0 | 5.0 |
| Tofu bílé | 125 | 12.0 | 0.85 | 10.2 | 2.3 | 7.5 |
| Čočka hnědá (suchá) | 353 | 24.0 | 0.75 | 18.0 | 60.0 | 1.0 |
| Cizrna (suchá) | 364 | 19.0 | 0.75 | 14.25 | 61.0 | 6.0 |
| Rýže jasmínová (suchá) | 355 | 7.0 | 0.70 | 4.9 | 79.0 | 0.7 |
| Ovesné vločky | 370 | 13.5 | 0.70 | 9.45 | 58.5 | 7.0 |
| Těstoviny semolinové | 350 | 12.0 | 0.70 | 8.4 | 72.0 | 1.5 |
| Brambory | 77 | 2.0 | 0.80 | 1.6 | 17.0 | 0.1 |
| Banán | 89 | 1.1 | 0.70 | 0.77 | 23.0 | 0.3 |
| Olivový olej | 884 | 0.0 | 1.00 | 0.0 | 0.0 | 100.0 |
| Arašídové máslo | 588 | 25.0 | 0.65 | 16.25 | 20.0 | 50.0 |

---

## 2. Standardní převodní jednotky na gramy

Pokud uživatel specifikuje jednotku `ks` nebo běžnou míru, kód použije tyto fixní hmotnosti:

- `1 ks vejce` = 55 g
- `1 ks banán` = 120 g
- `1 ks jablko` = 150 g
- `1 lžíce oleje` = 13 g
- `1 lžička oleje` = 5 g
- `1 odměrka proteinu` = 30 g

---

## 3. Pravidla pro fuzzy matching potravin

Párování vstupu z LLM na lokální databázi zajišťuje knihovna `rapidfuzz`:

1. **Předzpracování**: Vstupní řetězec i názvy v databázi se převedou na malá písmena a odstraní se nadbytečné mezery.
2. **Algoritmus**: Používá se metrika `rapidfuzz.fuzz.token_sort_ratio`.
3. **Prahová hodnota skóre**:
   - Skóre $\ge 85$: Okamžitá automatická shoda.
   - Skóre $75 - 84$: Přijatelná shoda, systém zaloguje varování o nejistém mapování.
   - Skóre $< 75$: Shoda se zamítá, program vyvolá výjimku `FoodNotFoundError`.
4. **Slovník synonym (tvrdé mapování před spuštěním metriky)**:
   - `"kuřecí"` $\rightarrow$ `"kuřecí prsa"`
   - `"hovězí"` $\rightarrow$ `"hovězí zadní"`
   - `"vločky"` $\rightarrow$ `"ovesné vločky"`
   - `"protein"` $\rightarrow$ `"syrovátkový protein (80%)"`
   - `"tvaroh"` $\rightarrow$ `"tvaroh odtučněný"`