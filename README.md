# Smart Kraken – analiza opisu inwestycji lokalnym LLM

Aplikacja webowa (ciemny motyw, zielone akcenty), która przyjmuje **tekst w języku naturalnym**
(opis zadania, fragment uchwały, notatkę, ogłoszenie, e-mail) i za pomocą **lokalnego, otwartego
modelu językowego** wyciąga z niego:

| Pole                 | Postać wyniku                                                              |
|----------------------|----------------------------------------------------------------------------|
| Zamawiający          | pełna nazwa                                                                |
| Rodzaj zamówienia    | roboty budowlane / dostawy / usługi (+ kod BZP: Works / Delivery / Services) |
| Miejscowość          | w mianowniku                                                               |
| Województwo          | jedno z 16 (+ kod PL02–PL32); oznaczone, gdy model je **wywnioskował**     |
| Planowana kwota      | liczba, waluta, netto / brutto (np. „4,2 mln zł” → 4200000)               |
| Planowany czas       | liczba + jednostka i/lub termin końcowy, przeliczone na dni                |
| Słowa kluczowe       | 6–15 fraz z kategoriami: zakres, obiekt, technologia, lokalizacja, finansowanie, harmonogram, formuła, ryzyko |

Każda wartość ma **cytat z tekstu**. Serwer sprawdza, czy cytat naprawdę występuje w tekście
(„✓ w tekście” / „≈ w tekście” / „sprawdź”), a strona podświetla źródła w tekście – kliknięcie
cytatu lub słowa kluczowego pokazuje je w opisie. Wyniki można poprawić ręcznie i wyeksportować
jako JSON. **Tekst nie opuszcza komputera.**

## Uruchomienie

1. Zainstaluj [Ollamę](https://ollama.com/download) i pobierz model (domyślnie polski Bielik od SpeakLeash):

   ```
   ollama pull SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M
   ```

2. Uruchom aplikację:

   ```
   python serwer.py          # albo: uv run serwer.py
   ```

   Przeglądarka otworzy http://127.0.0.1:8000. Serwer korzysta wyłącznie z biblioteki standardowej Pythona.

### Inne modele i serwery

| Polecenie | Kiedy |
|-----------|-------|
| `python serwer.py --model SpeakLeash/bielik-11b-v3.1-instruct:Q4_K_M` | nowsza wersja Bielika |
| `python serwer.py --model SpeakLeash/bielik-4.5b-v3.0-instruct:Q8_0` | słabszy komputer (mniejszy model) |
| `python serwer.py --model qwen2.5:7b-instruct` | dowolny inny model z Ollamy |
| `python serwer.py --backend openai --llm-url http://127.0.0.1:8080 --model NAZWA` | llama.cpp (`llama-server`), LM Studio, vLLM, LocalAI – serwery zgodne z API OpenAI |

Pozostałe opcje: `--llm-url` (adres serwera modelu), `--port`, `--host`, `--no-browser`.
Ustawienia domyślne są też na początku `serwer.py` (`BACKEND`, `LLM_URL`, `MODEL`, `MAX_TEXT_CHARS`…).

Model 11B w kwantyzacji Q4 potrzebuje ok. 7–8 GB pamięci. Bez karty graficznej analiza trwa
od kilkunastu sekund do kilku minut, zależnie od długości tekstu.

## Jak to działa

- `POST /api/extract` – serwer wysyła tekst do modelu z instrukcją po polsku i **schematem JSON**
  (Ollama: pole `format`, serwery OpenAI: `response_format` z `json_schema`, a gdy go nie obsługują –
  `json_object`). Temperatura 0, okno kontekstu dobierane do długości tekstu (do 32k tokenów).
- Odpowiedź jest porządkowana po stronie serwera: normalizacja województwa i rodzaju zamówienia,
  przeliczenie kwot („4,2 mln zł”, „850 000,50”), czasu na dni, weryfikacja cytatów i ich pozycji
  w tekście (także dla odmienionych słów: „kanalizacja grawitacyjna” ↔ „kanalizacji grawitacyjnej”).
- `GET /api/status` – czy serwer modelu działa i czy model jest pobrany (widoczne w nagłówku strony).
- Serwer udostępnia tylko `index.html` i `assets/` – pozostałe pliki folderu nie są dostępne z przeglądarki.

## Zapis debug

Każda analiza jest zapisywana do folderu `debug/` (pomijany przez git) jako
`RRRRMMDD-GGMMSS-mmm_model.json`, także gdy wystąpi błąd. Plik zawiera:

| Pole | Zawartość |
|------|-----------|
| `tekst` | analizowany tekst |
| `trace.request` | dokładne zapytanie do modelu (adres, instrukcja systemowa, schemat, opcje) |
| `trace.response` | pełna odpowiedź serwera modelu (u Ollamy także czasy i liczby tokenów) |
| `trace.content` | surowa treść wygenerowana przez model |
| `odpowiedz_sparsowana` | treść odczytana jako JSON |
| `wynik_po_obrobce` | wynik po normalizacji i sprawdzeniu cytatów (to, co widać na stronie) |
| `status`, `blad`, `wyjatek` | przy błędzie: co poszło nie tak |

Na stronie pod wynikiem jest nazwa pliku i rozwijana „Surowa odpowiedź modelu (debug)”.
Opcje: `--debug-dir FOLDER` (inny folder), `--no-debug` (bez zapisu).

## Struktura

```
serwer.py         serwer aplikacji + komunikacja z modelem + porządkowanie wyników
index.html        strona
assets/app.js     logika strony (analiza, podświetlanie, edycja, eksport)
assets/style.css  wygląd
```

Wyniki modelu językowego wymagają weryfikacji – dlatego każde pole ma cytat i oznaczenie źródła.
