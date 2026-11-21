# Harvestery źródeł zewnętrznych (skrypty pomocnicze)

Skrypty, które zasiliły katalog `data_lake/` (z manifestem SHA-256 każdego pobrania). Zasoby są wczytywane do bazy
poleceniem `python -m riskminer extras` (albo przyciskiem „Wczytaj źródła z data_lake” w zakładce Import danych).

| Skrypt | Co pobiera |
|---|---|
| `harvest_all_sources.py` | KIO (otwarte API SAOS), KPOŚK/AKPOŚK (API dane.gov.pl), NIK (RSS + strony kontroli + PDF), RIO (wystąpienia pokontrolne), BK2021, BZP produkcyjne – inne sektory (z limitem 30 zapytań/min) |
| `harvest_external_dbs.py` | TED (API v3, wielosektorowo), NIK, import plików |
| `bzp_mirror.py` | lokalny serwer z tym samym interfejsem co API BZP (kopia hackathonowa) – `python bzp_mirror.py` |
| `summarize_bzp.py` | statystyki kopii BZP |
| `poc/` | wczesne prototypy (PoC) wyszukiwania i wzbogacania – zastąpione pakietem `riskminer/` |

Przykład: `python harvest_all_sources.py --kio --kposk --out ../../../risk_miner_bzp_kopia/data_lake`
