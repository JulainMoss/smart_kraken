# Risk Miner – scenariusz pokazu i test o 11:00

## Test jury (11:00–11:45) – krok po kroku

Jury przekazuje dwa pliki: **rekord dodatkowego projektu** (jak w `Risk_Miner_dane_projekty.xlsx`) i **opis nowej inwestycji**
(kilka stron tekstu). Oddajemy dwa pliki tekstowe: **eksport rekordu projektu** i **analizę nowej inwestycji**. Bez zmian w kodzie.

**Przed 11:00 (5 min):**

```bash
cd smart_kraken
python -m riskminer serve              # http://127.0.0.1:8000 – sprawdź w nagłówku: liczba postępowań i zielona kropka modelu
# model: Claude przez Claude Code (config.toml: provider = "claude-code") – wymaga zalogowanego `claude`;
# alternatywa offline: scripts/start_llm.sh + provider = "openai". Bez modelu działają reguły eksperckie.
```

**O 11:00 – w przeglądarce (zalecane):**

1. Zakładka **Przetwórz pliki**.
2. Przeciągnij **rekord projektu** (XLSX/CSV) do pola „1. Rekord projektu”.
3. Przeciągnij **opis inwestycji** (DOCX/PDF/TXT) do pola „2. Opis nowej inwestycji” (albo wklej tekst).
4. **Przetwórz i przygotuj pliki** (ok. 1 min ze źródłami zewnętrznymi; postęp widać w dzienniku).
5. Pobierz dwa pliki główne:
   * `1_rekord_projektu_<ID>.md` – eksport rekordu projektu (powiązania z BZP/TED/KIO/BIP/NIK z podstawą i pewnością,
     przebieg, zmiany umów, zdarzenia ryzyka z cytatami, braki w danych, walidacja identyfikatorów),
   * `2_analiza_inwestycji_<AN-ID>.md` (lub `.pdf` – PDF z tekstem) – analiza nowej inwestycji.
   Te same wyniki są też w JSON i CSV, a wszystko razem – w ZIP. Pliki leżą w `data/wyniki/<przebieg>/`.
6. Wgraj pliki na stronę podaną przez organizatorów.
7. Dla pewności otwórz **projekt** i **raport** z przycisków pod wynikami – sprawdź, czy dane mają sens (np. niepewne powiązania).

**Plan B – terminal (to samo, bez przeglądarki):**

```bash
python -m riskminer test-run --project rekord.xlsx --investment opis.docx --out wyniki_testu/
```

**Gdy coś nie działa:**

| Objaw | Co zrobić |
|---|---|
| Czerwona kropka modelu w nagłówku | Nic – opis odczytają reguły. Sprawdź `claude --version` i logowanie (`claude`, `/login`). |
| Wyczerpany limit subskrypcji Claude | Odczyt wróci do reguł automatycznie (komunikat przy formularzu); albo `provider = "none"`. |
| Brak internetu | Odznacz „przeszukaj źródła zewnętrzne”; łączenie z lokalną bazą BZP działa offline. |
| Projekt bez powiązania BZP | Raport rekordu pokazuje „brak”/„niepewne” z uzasadnieniem – to poprawny wynik; ekspert może zatwierdzić kandydata. |
| Plik opisu to skan PDF | Odczyt przez OCR (tesseract), jeśli zainstalowany; inaczej wklej tekst w polu „albo wklej tekst opisu”. |

## Pokaz dla jury (ok. 5 min)

1. **Nowa analiza → przykład „Opis potoczny, jak w mailu”** – urzędnik pisze jak w mailu; model językowy (Claude) wypełnia
   formularz, przy każdej wartości widać cytat z opisu i źródło (model / reguły / zgodne). Rozbieżności są oznaczone.
2. **Wygeneruj raport** – ryzyko opóźnienia z przedziałem 90% i N, rzeczywisty czas, zmiana kosztu, ocena budżetu na km,
   sygnały ostrzegawcze; zakładka **Problemy** – każdy wniosek ma cytat i numer ogłoszenia (klik → treść ogłoszenia z podświetleniem).
3. **Uniwersalność – przykłady „Inne sektory”** (droga powiatowa, most, termomodernizacja szkoły, fotowoltaika): sektor rozpoznany
   z opisu, porównanie z 25 zakończonymi inwestycjami tego samego rodzaju (np. przebudowy dróg powiatowych), koszt na km drogi,
   ryzyka i rekomendacje. W bazie: 4,2 tys. postępowań drogowych, 1,3 tys. budynków, energetyka, kolej.
4. **Przykład „Zbyt mały budżet i zbyt krótki termin”** – czerwone flagi: budżet/km niższy niż w 82% podobnych, termin krótszy
   niż mediana.
5. **Do kogo się zwrócić** – gminy, które robiły to samo (z sukcesem lub z problemami), z gotowym e-mailem.
6. **Projekty → RM-2026-022 (Wolin)** – numer BZP z rekordu nie istnieje; system wykrył błąd i odnalazł postępowanie po odwołaniach.
7. **Weryfikacja ekspercka** – zatwierdź/odrzuć niepewne powiązanie; zmień kategorię zdarzenia; historia decyzji.
8. **Import danych → Dodaj dane z BIP** – wklej adres strony BIP (np. zapytanie ofertowe z załącznikami); system pobiera stronę
   i załączniki, zapisuje źródło i SHA-256, wskazuje fragmenty ryzyka do weryfikacji.
9. **Przetwórz pliki** – ścieżka testu jury jednym kliknięciem.

## Przykładowe opisy do pokazu (wbudowane w „Nowa analiza → Przykłady”)

| Przykład | Co pokazuje |
|---|---|
| **Kanalizacja w gminie wiejskiej – pełny opis** (Kłoda, 3,6 km + 1,2 km, ZiW, 4,2 mln netto, 14 mies.) | pełny raport: Natura/grunty prywatne/przewiert, R06/R07/R03 z dowodami |
| **Zbyt mały budżet i zbyt krótki termin** (15 km za 3 mln zł w 6 mies.) | czerwone flagi budżetu i terminu |
| **Opis potoczny, jak w mailu** („chcemy… w naszej gminie… rok na realizację”) | rozumienie języka potocznego (model + reguły) |
| **Oczyszczalnia – zaprojektuj i wybuduj** (Wadowice, 1200 m³/d, 24 mln) | inny podsektor, ryzyko PFU i rozruchu, 56% opóźnień |
| **Stacja uzdatniania wody z wodociągiem** (Juchnowiec Kościelny) | dopasowanie do wcześniejszych inwestycji tej samej gminy, termin krótszy niż u 75% |
| **Rzadka technologia: kanalizacja podciśnieniowa** | wyszukiwanie po znaczeniu – znajduje stację próżniową (Abramów) |
| **Kanalizacja deszczowa i retencja** (Łowicz) | podsektor deszczowy, wysoki udział robót dodatkowych |
| **Przebudowa drogi powiatowej z chodnikiem** (3,2 km, 9 mln, ZiW) | sektor drogowy: porównanie z przebudowami dróg, koszt na km drogi |
| **Budowa mostu na rzece** (42 m, 7 mln) | mosty: 5 najbliższych to przebudowy mostów, pozwolenie wodnoprawne (R04) |
| **Termomodernizacja szkoły** (4,5 mln, 10 mies.) | budynki: termomodernizacje szkół, roboty dodatkowe w 56% umów |
| **Fotowoltaika na budynkach gminy** (600 kW, 3,8 mln) | energetyka/OZE: termin krótszy niż u 75% podobnych instalacji |

Opis zbyt ogólny (np. „Proszę o pomoc, planujemy jakąś inwestycję w naszej gminie”) daje czerwony komunikat z prośbą
o uzupełnienie – system nie udaje pewności.

**Do wklejenia w „Dodaj dane z BIP” (zakładka Import danych):**
`https://bip.goscieradow.pl/wiadomosci/12162/wiadomosc/461134/zapytanie_ofertowe_na_modernizacje_oczyszczalni_w_wolce_gosciera`
(zapytanie ofertowe z 6 załącznikami: przedmiar, wzór umowy, informacja z otwarcia ofert, wybór oferty).
