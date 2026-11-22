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
| Planowany czas       | czas trwania, **data podpisania umowy**, termin zakończenia, termin pierwotny (przy aneksie); liczba dni realizacji |
| Zakres rzeczowy      | **ilości robót w liczbach**: element (stała lista), ilość, jednostka, parametry (DN, materiał) – np. 3,6 km kanalizacji grawitacyjnej DN 200 |
| Słowa kluczowe       | 6–10 fraz z kategoriami: zakres, obiekt, technologia, lokalizacja, finansowanie, harmonogram, formuła, ryzyko |

Model ma polecenie **nie podawać adresów** (ulic, numerów, kodów pocztowych, e-maili, stron www,
telefonów) w żadnym polu ani słowie kluczowym; serwer dodatkowo usuwa adres, jeśli model go jednak poda.

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
   uv run serwer.py          # instaluje też pypdf i Pillow (odczyt PDF)
   ```

   albo bez uv:

   ```
   pip install pypdf         # jednorazowo, potrzebne tylko do plików PDF
   python serwer.py
   ```

   Przeglądarka otworzy http://127.0.0.1:8000. Poza pypdf serwer korzysta wyłącznie z biblioteki standardowej Pythona.

### Inne modele i serwery

| Polecenie | Kiedy |
|-----------|-------|
| `python serwer.py --model SpeakLeash/bielik-11b-v3.1-instruct:Q4_K_M` | nowsza wersja Bielika |
| `python serwer.py --model SpeakLeash/bielik-4.5b-v3.0-instruct:Q8_0` | słabszy komputer (mniejszy model) |
| `python serwer.py --model qwen2.5:7b-instruct` | dowolny inny model z Ollamy |
| `python serwer.py --backend openai --llm-url http://127.0.0.1:8080 --model NAZWA` | llama.cpp (`llama-server`), LM Studio, vLLM, LocalAI – serwery zgodne z API OpenAI |

Pozostałe opcje: `--llm-url` (adres serwera modelu), `--port`, `--host`, `--no-browser`.
Ustawienia domyślne są też na początku `serwer.py` (`BACKEND`, `LLM_URL`, `MODEL`, `CTX`, `MAX_TOKENS`, `KEEP_ALIVE`…).

### Wydajność

| Opcja | Domyślnie | Po co |
|-------|-----------|-------|
| `--ctx N` | 8192 | **stałe** okno kontekstu (przycinane do maksimum modelu). Zmiana rozmiaru między zapytaniami zmusza Ollamę do przeładowania modelu, więc rozmiar się nie zmienia. Wyznacza też, ile tekstu zmieści się w analizie (przy 8192: ok. 14 tys. znaków – licznik pod polem pokazuje limit). |
| `--max-tokens N` | 1536 | limit długości odpowiedzi – mały model, który się zapętli, zostaje zatrzymany zamiast liczyć minutami |
| `--keep-alive CZAS` | 30m | jak długo Ollama trzyma model w pamięci po analizie (`-1` = zawsze, `0` = od razu zwalnia) |

Model odpowiada strumieniowo, a „Przerwij” (albo zamknięcie karty) przerywa połączenie z Ollamą,
więc model **przestaje liczyć od razu**. Cytaty są krótkie (do 15 słów), a słów kluczowych najwyżej 10 –
odpowiedź jest krótsza, więc analiza szybsza.

Model 11B w kwantyzacji Q4 potrzebuje ok. 7–8 GB pamięci. Bez karty graficznej analiza trwa
od kilkunastu sekund do kilku minut, zależnie od długości tekstu.

## Jak to działa

- `POST /api/extract` – serwer wysyła tekst do modelu z instrukcją po polsku i **schematem JSON**
  (Ollama: pole `format`, serwery OpenAI: `response_format` z `json_schema`, a gdy go nie obsługują –
  `json_object`). Temperatura 0, okno kontekstu dobierane do długości tekstu (do 32k tokenów).
- Odpowiedź jest porządkowana po stronie serwera: normalizacja województwa i rodzaju zamówienia,
  przeliczenie kwot („4,2 mln zł”, „850 000,50”), czasu na dni, weryfikacja cytatów i ich pozycji
  w tekście (także dla odmienionych słów: „kanalizacja grawitacyjna” ↔ „kanalizacji grawitacyjnej”).
- `POST /api/file` – odczyt pliku PDF / DOCX / ODT do tekstu.
- `GET /api/status` – czy serwer modelu działa i czy model jest pobrany (widoczne w nagłówku strony).
- Serwer udostępnia tylko `index.html` i `assets/` – pozostałe pliki folderu nie są dostępne z przeglądarki.

## Weryfikacja odpowiedzi modelu

Małe modele potrafią pomylić pola (np. wpisać kwotę jako zamawiającego) albo przepisać coś z instrukcji.
Dlatego serwer sprawdza każdą odpowiedź z tekstem i pokazuje na stronie, co odrzucił i dlaczego:

- **zamawiający i miejscowość** muszą występować w tekście i nie mogą być kwotą, liczbą ani „brak”;
  gdy model się pomyli, zamawiający jest odczytywany regułą (etykieta „Zamawiający:”, „Gmina/Miasto/Powiat …”),
- **województwo** wymienione w tekście (w dowolnej formie, np. „Województwa Podkarpackiego”) ma pierwszeństwo;
  jeśli w tekście go nie ma, wartość modelu jest oznaczona jako wniosek do sprawdzenia,
- **kwota** musi występować w tekście (z uwzględnieniem „mln”, „tys.”),
- **daty** muszą występować w tekście (rozpoznawane zapisy: 14.02.2025, 14 lutego 2025, „do końca czerwca 2026”,
  „IV kwartał 2026” …); data podpisania („umowa … z dnia …”), termin dotychczasowy i nowy są w razie potrzeby
  odczytywane regułą,
- **czas trwania** musi występować w tekście razem z jednostką („20 miesięcy”, „trzech miesięcy”).

Model dostaje polecenie, by kwoty i daty **przepisywał tak, jak są w tekście** – przeliczenia (4,2 mln zł → 4 200 000,
14.02.2025 → 2025-02-14) robi serwer. Instrukcja dla modelu nie zawiera żadnych przykładowych nazw ani dat,
które mógłby skopiować.

## Czas realizacji

Gdy w tekście jest **data podpisania umowy**, czas liczony jest od niej:

- czas trwania + data podpisania → termin wyliczony (miesiące kalendarzowo: 14.02.2025 + 20 miesięcy = 14.10.2026),
- termin zakończenia + data podpisania → liczba dni realizacji (np. 14.02.2025–05.12.2025 = 294 dni),
- termin pierwotny i nowy (aneks, przedłużenie) → przedłużenie w dniach (np. +36 dni),
- rozbieżność między terminem a czasem trwania (ponad 15 dni, niewyjaśniona przedłużeniem) jest sygnalizowana.

Bez daty podpisania czas wynika z samego czasu trwania, a w ostateczności liczony jest od dziś – strona
zawsze pokazuje, na jakiej podstawie. Po poprawieniu dat na stronie wszystko przelicza się od razu.

## Zakres rzeczowy i porównywanie projektów

Model wypisuje każdy element robót podany z liczbą. Element i jednostka pochodzą ze **stałych list**
(np. „kanalizacja sanitarna grawitacyjna”, „przepompownia ścieków”; km, m, szt., m2, m3, m3/d, RLM, kW),
a serwer przelicza ilości na jednostki bazowe (km → m) i sumuje je według elementu. Każda pozycja ma
cytat; „sprawdź” oznacza, że liczby nie ma w cytacie albo cytatu nie ma w tekście (liczebniki słowne
typu „dwóch” są rozpoznawane). Pozycje można poprawiać, usuwać i dodawać.

Każda analiza dopisuje wiersz do **`wyniki/zestawienie.csv`** (separator `;`, przecinek dziesiętny,
otwiera się w Excelu): zamawiający, miejscowość, województwo, rodzaj, kwota, daty, czas realizacji w dniach,
przedłużenie, a dalej **stałe kolumny** z sumą dla każdego elementu, np. `sieć wodociągowa [m]`,
`przepompownia ścieków [szt.]`. Dzięki temu projekty można porównywać wprost (np. koszt na metr sieci,
dni na kilometr). Gdy zmieni się układ kolumn (nowa wersja aplikacji), powstaje nowy plik zestawienia.

## Wczytywanie plików

Przycisk „Wczytaj plik” (okno pokazuje wszystkie pliki) albo przeciągnięcie pliku na pole tekstu. Tekst trafia do pola, gdzie można
go przejrzeć i poprawić przed analizą. Pliki są czytane lokalnie przez `serwer.py` – nie opuszczają komputera.

| Format | Jak |
|--------|-----|
| `.pdf` | pakiet `pypdf` (albo program `pdftotext`, jeśli jest zainstalowany); łączy wyrazy przeniesione z dywizem; skany – OCR (niżej) |
| `.docx` | biblioteka standardowa: akapity, tabele (wiersz = linia, komórki rozdzielone „ \| ”), przypisy |
| `.odt` | biblioteka standardowa: akapity i tabele |
| `.txt`, `.md`, `.csv`, `.html` | w przeglądarce (UTF-8 albo Windows-1250) |

**Skany PDF** (strona zapisana jako obraz – da się w nich zaznaczać tekst tylko dlatego, że przeglądarka
PDF sama go rozpoznaje): serwer rozpoznaje tekst programem **Tesseract OCR**, lokalnie. Wystarczy go
zainstalować – w Windows z https://github.com/UB-Mannheim/tesseract/wiki, zaznaczając przy instalacji
język **Polish** – i uruchomić serwer ponownie (komunikat przy starcie pokazuje, czy OCR jest dostępne).
OCR działa tylko dla PDF bez warstwy tekstowej; zwykłe PDF czytane są jak dotąd. Rozpoznany tekst jest
oznaczony na stronie – warto go przejrzeć przed analizą. Opcje: `--tesseract ŚCIEŻKA` (gdy program jest
w nietypowym miejscu), `--ocr-lang pol+eng`. Bez Tesseracta aplikacja podpowiada, jak go zainstalować,
a tekst można skopiować z przeglądarki PDF i wkleić.

Nieobsługiwany: stary `.doc` (zapisz jako `.docx`). Maks. 30 MB pliku; do modelu trafia pierwsze 60 000 znaków (`MAX_TEXT_CHARS`), a mniejsze
modele mają mniejsze okno kontekstu (np. Bielik 1.5B – 8K tokenów, ok. 15–20 tys. znaków).

## Zapis wyników

Każda udana analiza jest zapisywana do folderu `wyniki/` (pomijany przez git) jako
`RRRRMMDD-GGMMSS_zamawiajacy.json`, np. `20261121-223541_gmina_rydzyna.json`. Plik ma ten sam
układ co „Pobierz JSON” na stronie: `zamawiajacy`, `rodzaj_zamowienia`(+`_kod`),
`miejscowosc`, `wojewodztwo`(+`_kod`, `_wywnioskowane`), `planowana_kwota`, `planowany_czas`,
`slowa_kluczowe`, `cytaty`, `model`, `data_analizy`, `czas_analizy_s` i `tekst`.

Zapisywana jest odpowiedź modelu (po obróbce); poprawki wprowadzone na stronie trafiają tylko do
„Kopiuj JSON” / „Pobierz JSON”. Opcje: `--results-dir FOLDER`, `--no-results`.

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
