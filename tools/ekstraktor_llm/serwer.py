#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart Kraken – analiza opisu inwestycji lokalnym modelem językowym (LLM).

Strona (index.html) przyjmuje tekst w języku naturalnym, a serwer przekazuje go
do lokalnego, otwartego modelu LLM, który wyciąga:
  zamawiającego, rodzaj zamówienia, miejscowość, województwo,
  planowaną kwotę, planowany czas realizacji i słowa kluczowe inwestycji.
Każda odpowiedź jest zapisywana jako plik JSON w folderze wyniki/.

Model działa lokalnie – tekst nie opuszcza komputera.

Obsługiwane serwery modeli:
  ollama  (domyślnie)  http://127.0.0.1:11434        – np. Bielik od SpeakLeash
  openai               dowolny serwer zgodny z API OpenAI: llama.cpp (llama-server),
                       LM Studio, vLLM, LocalAI…   – np. http://127.0.0.1:8080

Uruchomienie:
  ollama pull SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M
  python serwer.py
  python serwer.py --model qwen2.5:7b-instruct
  python serwer.py --backend openai --llm-url http://127.0.0.1:8080 --model bielik

Pliki: .txt/.md/.csv/.html czyta strona; .docx i .odt – serwer (biblioteka standardowa);
.pdf – serwer przez pakiet pypdf (pip install pypdf; uv run instaluje go sam) albo program pdftotext.
PDF bez warstwy tekstowej (skan, zdjęcie strony) – OCR programem Tesseract z językiem polskim, jeśli jest
zainstalowany (Windows: https://github.com/UB-Mannheim/tesseract/wiki, przy instalacji zaznacz „Polish”).

Wymagania: Python 3.8+; do PDF dodatkowo pypdf.
"""

import argparse
import csv
import io
import os
import select
import socket
import uuid
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
import datetime as dt
import http.server
import json
import re
import socketserver
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = "2026-11-22.5"   # musi się zgadzać z APP_VERSION w assets/app.js (strona ostrzega o starym serwerze)

# ---- ustawienia (można zmienić tutaj albo opcjami wiersza poleceń) -------------------
BACKEND = "ollama"                                         # --backend: ollama | openai
LLM_URL = "http://127.0.0.1:11434"                         # --llm-url
MODEL = "SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M"       # --model
TEMPERATURE = 0.0
LLM_TIMEOUT_S = 600            # najdłuższa przerwa w odpowiedzi modelu (CPU bywa wolne)
MAX_TEXT_CHARS = 60000         # bezwzględny limit tekstu (realny limit wynika z okna kontekstu – niżej)
CTX = 8192                     # --ctx: STAŁE okno kontekstu (zmiana między zapytaniami = przeładowanie modelu);
                               #        przycinane do maksimum modelu (np. Bielik 1.5B: 8192)
MAX_TOKENS = 1536              # --max-tokens: limit długości odpowiedzi (zabezpieczenie przed zapętleniem modelu)
KEEP_ALIVE = "30m"             # --keep-alive: jak długo Ollama trzyma model w pamięci po analizie
PROMPT_RESERVE_TOKENS = 1400   # instrukcja + schemat (ok. 1000 tokenów) z zapasem
CHARS_PER_TOKEN = 2.3          # ostrożny przelicznik dla polskiego tekstu
DEBUG_DIR = ROOT / "debug"     # zapis każdego zapytania i odpowiedzi modelu (--debug-dir, --no-debug)
RESULTS_DIR = ROOT / "wyniki"  # wynik każdej analizy (wyciągnięte pola) jako JSON (--results-dir, --no-results)

MAX_FILE_BYTES = 30 * 1024 * 1024   # największy przyjmowany plik PDF/DOCX/ODT
TESSERACT = None               # --tesseract: ścieżka do tesseract.exe (domyślnie szukany w PATH i w Program Files)
OCR_LANG = "pol"               # --ocr-lang: język rozpoznawania (pol = polski; np. "pol+eng")
OCR_MAX_PAGES = 30             # OCR trwa ok. 2–5 s na stronę – dłuższe skany są przycinane

STATIC_FILES = {"/index.html"}
STATIC_DIRS = ("/assets/",)

# ---- słowniki -------------------------------------------------------------------------
WOJEWODZTWA = {
    "dolnośląskie": "PL02", "kujawsko-pomorskie": "PL04", "lubelskie": "PL06", "lubuskie": "PL08",
    "łódzkie": "PL10", "małopolskie": "PL12", "mazowieckie": "PL14", "opolskie": "PL16",
    "podkarpackie": "PL18", "podlaskie": "PL20", "pomorskie": "PL22", "śląskie": "PL24",
    "świętokrzyskie": "PL26", "warmińsko-mazurskie": "PL28", "wielkopolskie": "PL30",
    "zachodniopomorskie": "PL32",
}
RODZAJE = {"roboty budowlane": "Works", "dostawy": "Delivery", "usługi": "Services"}
KATEGORIE = ["zakres", "obiekt", "technologia", "lokalizacja", "finansowanie", "harmonogram", "formuła", "ryzyko", "inne"]
JEDNOSTKI = ["dni", "tygodnie", "miesiące", "lata"]

# Zakres rzeczowy: stałe listy, żeby dało się porównywać projekty między sobą.
ELEMENTY = {   # element: jednostki bazowe, w których sumujemy go w zestawieniu
    "kanalizacja sanitarna grawitacyjna": ["m"],
    "kanalizacja sanitarna tłoczna": ["m"],
    "kanalizacja sanitarna (inna/nieokreślona)": ["m"],
    "kanalizacja deszczowa": ["m"],
    "sieć wodociągowa": ["m"],
    "przyłącza kanalizacyjne": ["szt.", "m"],
    "przyłącza wodociągowe": ["szt.", "m"],
    "przepompownia ścieków": ["szt."],
    "oczyszczalnia ścieków": ["szt.", "RLM", "m3/d"],
    "stacja uzdatniania wody": ["szt.", "m3/d"],
    "zbiornik": ["szt.", "m3"],
    "studnia głębinowa": ["szt."],
    "studnie/studzienki": ["szt."],
    "urządzenia i wyposażenie": ["szt."],
    "nawierzchnie i drogi": ["m2", "m"],
    "inne": [],
}
JEDNOSTKI_ZAKRESU = ["km", "m", "szt.", "m2", "m3", "m3/d", "RLM", "kW", "inne"]
BAZA = {"km": ("m", 1000.0)}   # przeliczenia na jednostkę bazową; reszta bez zmian


def _field(desc):
    return {"type": "object", "properties": {"wartosc": {"type": "string"}, "cytat": {"type": "string"}},
            "required": ["wartosc", "cytat"], "description": desc}


# Wszystkie pola są tekstowe („” = brak informacji) – działa niezawodnie z każdym backendem.
SCHEMA = {
    "type": "object",
    "properties": {
        "zamawiajacy": _field("pełna nazwa zamawiającego / inwestora"),
        "rodzaj_zamowienia": {
            "type": "object",
            "properties": {"wartosc": {"type": "string", "enum": ["roboty budowlane", "dostawy", "usługi", ""]},
                           "cytat": {"type": "string"}},
            "required": ["wartosc", "cytat"],
        },
        "miejscowosc": _field("miejscowość, w której realizowana jest inwestycja"),
        "wojewodztwo": {
            "type": "object",
            "properties": {"wartosc": {"type": "string", "enum": list(WOJEWODZTWA) + [""]},
                           "cytat": {"type": "string"},
                           "wywnioskowane": {"type": "boolean"}},
            "required": ["wartosc", "cytat", "wywnioskowane"],
        },
        "planowana_kwota": {
            "type": "object",
            "properties": {"kwota": {"type": "string"}, "waluta": {"type": "string"},
                           "vat": {"type": "string", "enum": ["netto", "brutto", "nieokreślone"]},
                           "cytat": {"type": "string"}},
            "required": ["kwota", "waluta", "vat", "cytat"],
        },
        "planowany_czas": {
            "type": "object",
            "properties": {"liczba": {"type": "string"},
                           "jednostka": {"type": "string", "enum": JEDNOSTKI + [""]},
                           "data_podpisania": {"type": "string"},
                           "termin_do": {"type": "string"},
                           "termin_pierwotny": {"type": "string"},
                           "cytat": {"type": "string"}},
            "required": ["liczba", "jednostka", "data_podpisania", "termin_do", "termin_pierwotny", "cytat"],
        },
        "zakres_rzeczowy": {
            "type": "array",
            "maxItems": 15,
            "items": {"type": "object",
                      "properties": {"element": {"type": "string", "enum": list(ELEMENTY)},
                                     "opis": {"type": "string"},
                                     "ilosc": {"type": "string"},
                                     "jednostka": {"type": "string", "enum": JEDNOSTKI_ZAKRESU},
                                     "parametry": {"type": "string"},
                                     "cytat": {"type": "string"}},
                      "required": ["element", "opis", "ilosc", "jednostka", "parametry", "cytat"]},
        },
        "slowa_kluczowe": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "object",
                      "properties": {"fraza": {"type": "string"},
                                     "kategoria": {"type": "string", "enum": KATEGORIE}},
                      "required": ["fraza", "kategoria"]},
        },
    },
    "required": ["zamawiajacy", "rodzaj_zamowienia", "miejscowosc", "wojewodztwo",
                 "planowana_kwota", "planowany_czas", "zakres_rzeczowy", "slowa_kluczowe"],
}

SYSTEM_PROMPT = """Jesteś asystentem analizującym opisy inwestycji i zamówień publicznych w Polsce.
Z tekstu użytkownika wyciągasz dane i zwracasz WYŁĄCZNIE obiekt JSON zgodny ze schematem.

Najważniejsze zasady:
- Wszystkie wartości bierz WYŁĄCZNIE z tekstu użytkownika. Nie zgaduj i nie zmyślaj. Gdy czegoś nie ma – "".
- Ta instrukcja nie zawiera żadnych danych o inwestycji – nie przepisuj z niej niczego do odpowiedzi.
- "cytat" to DOSŁOWNY, KRÓTKI fragment tekstu użytkownika (najwyżej 15 słów), z którego pochodzi wartość; "" gdy brak.
- NIE PODAWAJ ADRESÓW: bez ulic, numerów budynków, kodów pocztowych, e-maili, stron www i telefonów.

Pola:
- zamawiajacy: NAZWA instytucji, która zamawia lub realizuje inwestycję (gmina, miasto, powiat, spółka wodociągowa,
  zakład komunalny…). To nigdy nie jest kwota, data, numer ani opis robót. Bez adresu.
- rodzaj_zamowienia: "roboty budowlane" (budowa, przebudowa, remont, modernizacja, „zaprojektuj i wybuduj”),
  "dostawy" (zakup sprzętu, materiałów, urządzeń), "usługi" (projekty, nadzór, utrzymanie, serwis).
- miejscowosc: główna miejscowość inwestycji, w mianowniku. Bez adresu.
- wojewodztwo: nazwa z listy, małymi literami. Jeśli nie pada w tekście, a jednoznacznie wynika z miejscowości
  lub zamawiającego – podaj ją i ustaw "wywnioskowane": true. Bez pewności – "".
- planowana_kwota: kwota inwestycji, zamówienia lub umowy.
  * kwota: PRZEPISZ kwotę dokładnie tak, jak jest w tekście, razem z „mln” lub „tys.”, jeśli są;
  * waluta: "PLN" dla zł; vat: "netto", "brutto" albo "nieokreślone".
- planowany_czas: daty PRZEPISZ dokładnie tak, jak są w tekście (w dowolnym formacie); "" gdy brak.
  * liczba + jednostka: czas realizacji, jeśli tekst podaje go jako okres (liczba dni, tygodni, miesięcy, lat);
  * data_podpisania: data zawarcia lub podpisania umowy (zwykle „umowa … z dnia …”);
  * termin_do: obowiązujący termin zakończenia – przy zmianie terminu (aneks, przedłużenie) ten NOWY;
  * termin_pierwotny: dotychczasowy termin, tylko jeśli tekst mówi o jego zmianie.
- zakres_rzeczowy: każdy element robót lub dostaw, dla którego tekst podaje LICZBĘ (długość, liczbę sztuk,
  powierzchnię, objętość, wydajność, RLM, moc). Jedna pozycja = jeden element i jedna liczba.
  * element: z listy ("inne", gdy nic nie pasuje); opis: krótko, co to jest;
  * ilosc: liczba z tekstu (z kropką dziesiętną), bez przeliczania jednostek; jednostka: z listy ("mb" to "m");
  * parametry: średnica, materiał, typ, "" gdy brak; cytat: dosłowny fragment z tą liczbą.
  Nie dodawaj pozycji bez liczby ani kwot pieniędzy. Gdy tekst nie podaje ilości – pusta lista.
- slowa_kluczowe: 6–10 krótkich fraz (1–5 słów) o najważniejszych cechach inwestycji: zakres robót, obiekty,
  technologie, formuła realizacji, finansowanie, terminy, ryzyka i problemy. Bez liczb i ilości (te trafiają
  do zakres_rzeczowy). Każdej przypisz kategorię z listy. Bez powtórzeń.

Lista województw: {woj}."""


# ---- pomocnicze -------------------------------------------------------------------------
def fold(s):
    s = (s or "").lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_ws(s):
    return re.sub(r"\s+", " ", fold(s)).strip(" .,;:\"'„”")


def check_quote(quote, text):
    """Czy cytat występuje w tekście: 'dokladny', 'czesciowy' (większość słów), 'brak'."""
    q = norm_ws(quote)
    if not q:
        return "brak"
    t = norm_ws(text)
    if q in t:
        return "dokladny"
    words = [w for w in re.findall(r"\w+", q) if len(w) > 2]
    if not words:
        return "brak"
    tw = set(re.findall(r"\w+", t))
    hit = sum(1 for w in words if w in tw) / len(words)
    return "czesciowy" if hit >= 0.75 else "brak"


def find_span(quote, text):
    """Pozycja cytatu w oryginalnym tekście (do podświetlenia) – dopasowanie bez wielkości liter i odstępów."""
    q = norm_ws(quote)
    if not q:
        return None
    # mapowanie znormalizowanego tekstu na indeksy oryginału
    chars, idx = [], []
    prev_space = True
    for i, ch in enumerate(text):
        f = fold(ch)
        if ch.isspace():
            if not prev_space:
                chars.append(" ")
                idx.append(i)
            prev_space = True
            continue
        for c in f or ch:
            chars.append(c)
            idx.append(i)
        prev_space = False
    t = "".join(chars)
    pos, end = t.find(q), None
    if pos >= 0:
        end = pos + len(q)
    else:
        # odmiana polska: „kanalizacja grawitacyjna” ~ „kanalizacji grawitacyjnej” – porównanie rdzeni słów
        words = re.findall(r"\w+", q)
        if not words or len(words) > 12:
            return None
        parts = [re.escape(w[:max(4, len(w) - 3)]) + r"\w*" if len(w) > 4
                 else re.escape(w[:3]) + r"\w*" if len(w) >= 3 else re.escape(w) for w in words]
        m = re.search(r"\b" + r"\W+".join(parts), t)
        if not m:
            return None
        pos, end = m.start(), m.end()
    return [idx[pos], idx[min(end, len(idx)) - 1] + 1]


MULT = [(r"mld|miliard", 1e9), (r"mln|milion", 1e6), (r"tys|tysi", 1e3)]


def parse_amount(s):
    """'4,2 mln zł' -> 4200000.0, '850 000,50' -> 850000.5, '4200000' -> 4200000.0"""
    if not s:
        return None
    s = str(s).lower().replace(" ", " ")
    m = re.search(r"\d[\d\s.,]*", s)
    if not m:
        return None
    num = m.group(0).strip().replace(" ", "")
    if "," in num and "." in num:          # 1.234.567,89
        num = num.replace(".", "").replace(",", ".")
    elif "," in num:
        num = num.replace(",", ".") if re.search(r",\d{1,2}$", num) or re.search(r"(mld|mln|tys|miliard|milion|tysi)", s) else num.replace(",", "")
    elif num.count(".") > 1 or re.search(r"\.\d{3}$", num) and not re.search(r"(mld|mln|tys)", s):
        num = num.replace(".", "")
    try:
        val = float(num.rstrip("."))
    except ValueError:
        return None
    rest = s[m.end():m.end() + 12]
    for pat, mul in MULT:
        if re.match(r"\s*(" + pat + ")", rest):
            val *= mul
            break
    return round(val, 2)


def norm_choice(value, options):
    f = fold(value).replace("woj.", "").replace("wojewodztwo", "").strip(" .")
    if not f:
        return ""
    for o in options:
        if fold(o) == f:
            return o
    for o in options:  # odmiana: „śląskim”, „wielkopolskiego”
        fo = fold(o)
        if len(f) >= 5 and (fo.startswith(f[:max(5, len(fo) - 3)]) or f.startswith(fo[:-3])):
            return o
    return ""


MIESIACE = {"styczn": 1, "styczeń": 1, "styczen": 1, "lut": 2, "marz": 3, "marc": 3, "kwie": 4, "maj": 5, "czerw": 6,
            "lip": 7, "sierp": 8, "wrze": 9, "paźdz": 10, "pazdz": 10, "listop": 11, "grud": 12}
_MONTH_RE = r"(styczni\w*|styczeń|styczen|lut\w*|marz\w*|marc\w*|kwie\w*|maj\w*|czerw\w*|lip\w*|sierp\w*|wrze\w*|paźdz\w*|pazdz\w*|listop\w*|grud\w*)"
_ROMAN_Q = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def _month_of(word):
    w = (word or "").lower()
    for k, v in MIESIACE.items():
        if w.startswith(k):
            return v
    return None


def _month_end(y, m):
    return (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))


def parse_date(s):
    """Data z tekstu w dowolnym typowym polskim zapisie: 2025-02-14, 14.02.2025 r., 14-02-2025, 14/02/2025,
    „14 lutego 2025”, „do końca czerwca 2026”, „do końca 2027 r.”, „IV kwartał 2026”."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)", s)
        if m:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = re.search(r"\b(\d{1,2})\s+" + _MONTH_RE + r"\s+(\d{4})", s, re.I)
        if m and _month_of(m.group(2)):
            return dt.date(int(m.group(3)), _month_of(m.group(2)), int(m.group(1)))
        m = re.search(r"\b(i{1,3}|iv|[1-4])\s*kw(?:arta\w*|\.)?\s*(\d{4})", s, re.I)
        if m:
            return _month_end(int(m.group(2)), _ROMAN_Q[m.group(1).lower()] * 3)
        m = re.search(r"\b" + _MONTH_RE + r"\s+(\d{4})", s, re.I)      # „do końca czerwca 2026”
        if m and _month_of(m.group(1)):
            return _month_end(int(m.group(2)), _month_of(m.group(1)))
        m = re.search(r"\bkońc\w*\s+(?:roku\s+)?(\d{4})|\b(\d{4})\s*r(?:oku|\.)?\b", s, re.I)
        if m and re.search(r"końc|koniec|do\s+\d{4}", s, re.I):
            return dt.date(int(m.group(1) or m.group(2)), 12, 31)
    except ValueError:
        return None
    return None


def date_in_text(d, text):
    """Czy data (w dowolnym zapisie) występuje w tekście – chroni przed datami wymyślonymi przez model."""
    if not d:
        return False
    t = fold(text)
    for f in (f"{d.day:02d}.{d.month:02d}.{d.year}", f"{d.day}.{d.month:02d}.{d.year}", f"{d.day}.{d.month}.{d.year}",
              f"{d.day:02d}-{d.month:02d}-{d.year}", f"{d.day:02d}/{d.month:02d}/{d.year}", d.isoformat()):
        if f in t:
            return True
    for m in re.finditer(r"\b(\d{1,2})\s+" + _MONTH_RE + r"\s+(\d{4})", text, re.I):
        if (int(m.group(1)), _month_of(m.group(2)), int(m.group(3))) == (d.day, d.month, d.year):
            return True
    # koniec miesiąca / kwartału / roku: „do końca czerwca 2026”, „do końca 2027 r.”, „IV kwartał 2026”, „do 2027 r.”
    if d == _month_end(d.year, d.month):
        y = str(d.year)
        if re.search(r"\bkon(?:c\w*|iec)\s+(?:\w+\s+){0,2}" + y, t) or re.search(r"\bdo\s+" + y + r"\b", t):
            return True
        if d.month in (3, 6, 9, 12) and re.search(r"\b(i{1,3}|iv|[1-4])\s*kw\w*\.?\s*" + y, t):
            return True
    return False


_DATE_PAT = r"(\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+" + _MONTH_RE + r"\s+\d{4})"


def duration_in_text(n, unit, text):
    """Czy okres „n jednostek” jest w tekście, z jednostką tuż za liczbą („20 miesięcy”, „trzech miesięcy”)."""
    stems = {"dni": r"(?:dni|dzie)", "tygodnie": r"(?:tyg|tydz)", "miesiące": r"(?:mies)", "lata": r"(?:lat|rok)"}.get(unit)
    if not stems:
        return False
    for m in re.finditer(r"(\d+(?:[.,]\d+)?|[a-ząćęłńóśźż]+)\s*" + stems, text.lower()):
        vals = _numbers_in(m.group(1))
        if any(abs(v - n) < 1e-6 for v in vals):
            return True
    return False


def find_contracting_authority(text):
    """Zamawiający odczytany z tekstu regułą: etykieta „Zamawiający:” albo „Gmina/Miasto/Powiat Nazwa”."""
    m = re.search(r"Zamawiaj\w*\s*:\s*([^\n]{3,150})", text)
    if m:
        v = strip_address(m.group(1).strip())
        if v and not _BAD_NAME.search(v):
            return v, m.start(1)
    cap = r"[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż-]+"
    m = re.search(r"\b((?:Gmin[ay]|Miast[oa]|Powiat(?:u)?|Związ\w+\s+Gmin)(?:\s+i\s+(?:Gmina|Miasto))?\s+" + cap + r"(?:\s+" + cap + r")?)", text)
    if m:
        return m.group(1).strip(), m.start(1)
    return None, None


def amounts_in_text(text):
    """Wszystkie kwoty/liczby z tekstu z mnożnikami (4,2 mln → 4200000)."""
    vals = []
    for m in re.finditer(r"\d+(?:[ \u00a0.]\d{3})*(?:,\d+)?(?:\.\d+)?\s*(mld|miliard\w*|mln|milion\w*|tys\w*)?", text or "", re.I):
        v = parse_amount(m.group(0))
        if v is not None:
            vals.append(v)
    return vals


# ---- adresy: model ma ich nie podawać (instrukcja), a to zabezpieczenie na wypadek, gdyby jednak podał ----
_STREET = r"(?:ul|al|pl|os|ulica|aleja|aleje|plac|osiedle|skwer|rondo)\.?\s+[^,;()\n]+"
_ADDR_PARTS = [
    r"\b(?:tel|fax|e-mail|email|adres|siedziba)\b\.?:?",        # etykiety danych kontaktowych
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",                        # e-mail
    r"(?:https?://|www\.)\S+",                                 # strona www
    r"(?:\+48[\s-]?)?(?:\(?\d{2,3}\)?[\s-]?)?\d{3}[\s-]?\d{2,3}[\s-]?\d{2,3}\b",   # telefon
    r"\b\d{2}-\d{3}\b",                                       # kod pocztowy
    r"\b" + _STREET,                                           # ulica z numerem
]
_ADDR_RE = re.compile("|".join(_ADDR_PARTS), re.I)
_ADDR_ONLY = re.compile(r"^\s*(?:" + "|".join(_ADDR_PARTS) + r")\s*$", re.I)


def strip_address(value):
    """Usuwa z nazwy/miejscowości adres: ulicę z numerem, kod pocztowy, e-mail, www, telefon."""
    if not value:
        return value
    m = _ADDR_RE.search(value)
    if not m:
        return value
    if m.start() > 0:                       # adres zwykle stoi po nazwie – utnij od niego
        out = value[:m.start()]
    else:                                   # adres na początku – usuń jego części, zostaw resztę
        out = _ADDR_RE.sub(" ", value)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,;:-–(")
    return out or value


def looks_like_address(phrase):
    return bool(_ADDR_ONLY.match(phrase or "")) or bool(re.match(r"^\s*" + _STREET, phrase or "", re.I))


# ---- czas realizacji ----------------------------------------------------------------------
def add_duration(start, n, unit):
    """Data końca: start + n jednostek. Miesiące i lata kalendarzowo (31.01 + 1 mies. = 28/29.02)."""
    if unit == "dni":
        return start + dt.timedelta(days=round(n))
    if unit == "tygodnie":
        return start + dt.timedelta(days=round(n * 7))
    months = n * 12 if unit == "lata" else n
    whole = int(months)
    y, m = divmod(start.month - 1 + whole, 12)
    y, m = start.year + y, m + 1
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    end = dt.date(y, m, min(start.day, last))
    return end + dt.timedelta(days=round((months - whole) * 30.4375))


def compute_time(liczba, jednostka, podpisanie, termin, pierwotny, today):
    """Czas realizacji. Z datą podpisania umowy liczy od niej (koniec = podpisanie + czas trwania,
    dni = koniec − podpisanie); bez niej – z samego czasu trwania, a w ostateczności od dziś."""
    jednostka = jednostka if jednostka in JEDNOSTKI and liczba is not None else ""
    if not jednostka:
        liczba = None
    out = {"liczba": liczba, "jednostka": jednostka,
           "data_podpisania": podpisanie.isoformat() if podpisanie else "",
           "termin_do": termin.isoformat() if termin else "",
           "termin_pierwotny": pierwotny.isoformat() if pierwotny else "",
           "termin_wyliczony": "", "dni": None, "podstawa": "", "przedluzenie_dni": None, "uwaga": ""}
    wyliczony = add_duration(podpisanie, liczba, jednostka) if (podpisanie and liczba is not None) else None
    if wyliczony:
        out["termin_wyliczony"] = wyliczony.isoformat()
    koniec = termin or wyliczony
    if podpisanie and koniec:
        out["dni"] = (koniec - podpisanie).days
        out["podstawa"] = "od podpisania umowy do " + ("terminu zakończenia" if termin else "terminu wyliczonego z czasu trwania")
    elif liczba is not None:
        out["dni"] = round(liczba * {"dni": 1, "tygodnie": 7, "miesiące": 30.4375, "lata": 365.25}[jednostka])
        out["podstawa"] = "z czasu trwania (brak daty podpisania umowy)"
    elif termin:
        out["dni"] = (termin - today).days
        out["podstawa"] = "od dziś do terminu zakończenia (brak daty podpisania umowy)"
    if termin and pierwotny:
        out["przedluzenie_dni"] = (termin - pierwotny).days
    explained = pierwotny and wyliczony and abs((pierwotny - wyliczony).days) <= 15   # różnicę tłumaczy przedłużenie
    if termin and wyliczony and abs((termin - wyliczony).days) > 15 and not explained:
        out["uwaga"] = (f"Termin zakończenia ({termin.isoformat()}) różni się o {abs((termin - wyliczony).days)} dni "
                        f"od wyliczonego z czasu trwania ({wyliczony.isoformat()}).")
    if out["dni"] is not None and out["dni"] < 0 and podpisanie:
        out["uwaga"] = "Termin zakończenia jest wcześniejszy niż data podpisania umowy – sprawdź daty."
    return out


# ---- zakres rzeczowy ----------------------------------------------------------------------
_NUM_RE = re.compile(r"\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?")


_WORDS = {  # liczebniki w różnych przypadkach (rdzenie) – „dwóch przepompowni”, „trzema zbiornikami”
    1: ["jeden", "jedna", "jedno", "jedneg", "jednej", "jednym", "jedną"], 2: ["dwa", "dwie", "dwóch", "dwoch", "dwom", "dwóm", "dwoma", "dwiema"],
    3: ["trzy", "trzech", "trzem", "trzema"], 4: ["cztery", "czterech", "czterem", "czterema"],
    5: ["pięć", "pięciu", "pięcioma"], 6: ["sześć", "sześciu", "sześcioma"], 7: ["siedem", "siedmiu", "siedmioma"],
    8: ["osiem", "ośmiu", "ośmioma"], 9: ["dziewięć", "dziewięciu"], 10: ["dziesięć", "dziesięciu"],
    11: ["jedenaście", "jedenastu"], 12: ["dwanaście", "dwunastu"], 15: ["piętnaście", "piętnastu"], 20: ["dwadzieścia", "dwudziestu"],
}
_WORD_RE = re.compile(r"\b(" + "|".join(sorted((w for ws in _WORDS.values() for w in ws), key=len, reverse=True)) + r")\w*", re.I)
_WORD_VAL = {w: v for v, ws in _WORDS.items() for w in ws}


def _numbers_in(s):
    out = [float(_WORD_VAL[m.group(1).lower()]) for m in _WORD_RE.finditer(s or "") if m.group(1).lower() in _WORD_VAL]
    for m in _NUM_RE.finditer(s or ""):
        try:
            out.append(float(m.group(0).replace(" ", "").replace("\u00a0", "").replace(",", ".")))
        except ValueError:
            pass
    return out


# jednostka tuż za liczbą: „3,6 km”, „1 200 mb”, „250 m²”, „40 m3/d”, „12 szt.”
_UNIT_RE = r"(km|m\.?\s?b\.?|metr\w*|m2|m²|m3\s*/\s*(?:d|dob\w*)|m³\s*/\s*(?:d|dob\w*)|m3|m³|m|szt\.?|sztuk\w*|RLM|kW)(?![a-ząćęłńóśźż0-9])"
_NUM_TXT = r"(\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)"


def _norm_unit(u):
    u = (u or "").lower().replace(" ", "").replace("²", "2").replace("³", "3")
    if u == "km":
        return "km"
    if u in ("m", "mb", "m.b.", "m.b", "mb.") or u.startswith("metr"):
        return "m"
    if u.startswith("m3/"):
        return "m3/d"
    if u in ("m2", "m3"):
        return u
    if u.startswith("szt"):
        return "szt."
    return {"rlm": "RLM", "kw": "kW"}.get(u)


def unit_after_number(n, quote):
    """Jednostka stojąca w cytacie tuż za liczbą n – ważniejsza niż jednostka wybrana przez model."""
    for m in re.finditer(_NUM_TXT + r"\s*" + _UNIT_RE, quote or "", re.I):
        v = parse_amount(m.group(1))
        if v is not None and abs(v - n) < 1e-6:
            return _norm_unit(m.group(2))
    return None


# rozpoznawanie elementu po słowach stojących za liczbą (reguła uzupełniająca odpowiedź modelu)
_EL_RULES = [
    (r"przyłącz\w*(?:\s+\w+){0,3}?\s+wod", "przyłącza wodociągowe"), (r"przyłącz", "przyłącza kanalizacyjne"),
    (r"tłoczn", "kanalizacja sanitarna tłoczna"), (r"grawitac", "kanalizacja sanitarna grawitacyjna"),
    (r"deszczow", "kanalizacja deszczowa"), (r"wodociąg|sieci\s+wodn|rurociąg\w*\s+wod", "sieć wodociągowa"),
    (r"przepompown", "przepompownia ścieków"), (r"oczyszczal", "oczyszczalnia ścieków"),
    (r"stacj\w*\s+uzdat|\bsuw\b", "stacja uzdatniania wody"), (r"zbiornik", "zbiornik"),
    (r"studni\w*\s+głębin", "studnia głębinowa"), (r"studz|studni", "studnie/studzienki"),
    (r"kanaliz|kanał|rurociąg|kolektor", "kanalizacja sanitarna (inna/nieokreślona)"),
    (r"nawierzchn|drog|chodnik|jezdni|kostk", "nawierzchnie i drogi"), (r"filtr|pomp|dmuchaw|agregat", "urządzenia i wyposażenie"),
]
_PARAM_RE = re.compile(r"\bDN\s?\d+|\bØ\s?\d+|\bPVC(?:-U)?\b|\bPEHD\b|\bPE(?:\s?\d+)?\b|\bPP\b|\bGRP\b|żeliw\w*|stal\w*|kamionk\w*|betonow\w*")
_COUNT_NOUNS = r"(przyłącz\w*|przepompown\w*|studzien\w*|studni\w*|zbiornik\w*|filtr\w*|pomp\w*|separator\w*|hydrant\w*|zasuw\w*)"
_WORDNUM = "|".join(sorted((w for ws in _WORDS.values() for w in ws), key=len, reverse=True))


def _classify(words):
    w = (words or "").lower()
    for pat, el in _EL_RULES:
        if re.search(pat, w):
            return el
    return None


def rule_scope(text):
    """Ilości znalezione w tekście regułą: „1,2 km rurociągu tłocznego PE DN 110”, „85 przyłączy”, „dwóch przepompowni”."""
    found = []
    for m in re.finditer(_NUM_TXT + r"\s*" + _UNIT_RE + r"((?:\s+(?!(?:oraz|lub|wraz|albo)\b)[\w./-]+){0,8})", text, re.I):
        qty, unit = parse_amount(m.group(1)), _norm_unit(m.group(2))
        el = _classify(m.group(3))
        if qty and unit and el and not re.match(r"\s*(n\.?\s?p\.?\s?m|npm|p\.p\.t)", m.group(3), re.I):
            found.append({"element": el, "ilosc": qty, "jednostka": unit, "span": [m.start(), m.end()],
                          "cytat": m.group(0).strip(), "parametry": ", ".join(dict.fromkeys(_PARAM_RE.findall(m.group(3))))})
    for m in re.finditer(r"(?<![\d,.])(\d+|" + _WORDNUM + r")\w*\s+(?:[\w-]+\s+){0,2}?" + _COUNT_NOUNS + r"((?:\s+(?!(?:oraz|i|a|lub|wraz|albo)\b)[^\W\d][\w./-]*){0,3})", text, re.I):
        nums = _numbers_in(m.group(1)) or ([float(m.group(1))] if m.group(1).isdigit() else [])
        el = _classify(m.group(2) + m.group(3))
        if nums and el and nums[0] > 0:
            found.append({"element": el, "ilosc": nums[0], "jednostka": "szt.", "span": [m.start(), m.end()],
                          "cytat": m.group(0).strip(), "parametry": ", ".join(dict.fromkeys(_PARAM_RE.findall(m.group(3))))})
    for f in found:  # cytat nie przechodzi przez koniec zdania
        cut = re.split(r"\.\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ])", f["cytat"])[0].rstrip(" .,;")
        f["span"][1] = f["span"][0] + len(cut) + (len(f["cytat"]) - len(f["cytat"].lstrip()))
        f["cytat"] = cut
    return found


def norm_scope(items, text):
    """Porządkuje zakres rzeczowy: liczby, jednostki (z cytatu, gdy model się pomylił), jednostki bazowe (km → m),
    sprawdzenie liczby w cytacie i cytatu w tekście; dokłada ilości z tekstu pominięte przez model."""
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        raw_qty = str(it.get("ilosc") or "")
        ilosc = parse_amount(raw_qty)
        if ilosc is None or ilosc <= 0:
            continue
        el = it.get("element") if it.get("element") in ELEMENTY else "inne"
        jedn = it.get("jednostka") if it.get("jednostka") in JEDNOSTKI_ZAKRESU else "inne"
        # jednostka z tekstu ma pierwszeństwo: model potrafi wpisać „3,6 km” w ilość, a jako jednostkę „m”
        u_txt = unit_after_number(ilosc, str(it.get("cytat") or "")) or unit_after_number(ilosc, raw_qty)
        if u_txt and u_txt in JEDNOSTKI_ZAKRESU and u_txt != jedn:
            it["_jednostka_modelu"] = jedn
            jedn = u_txt
        base_unit, mult = BAZA.get(jedn, (jedn, 1.0))
        cytat = str(it.get("cytat") or "").strip()
        opis = strip_address(str(it.get("opis") or "").strip()) or el
        key = (el, round(ilosc * mult, 3), base_unit, fold(opis))
        if key in seen:
            continue
        seen.add(key)
        nums = _numbers_in(cytat)
        item = {
            "element": el, "opis": opis, "ilosc": ilosc, "jednostka": jedn,
            "ilosc_bazowa": round(ilosc * mult, 3), "jednostka_bazowa": base_unit,
            "parametry": str(it.get("parametry") or "").strip(),
            "cytat": cytat,
            "liczba_w_cytacie": any(abs(n - ilosc) < 1e-6 or abs(n * mult - ilosc * mult) < 1e-6 for n in nums),
            "zrodlo": check_quote(cytat, text), "span": find_span(cytat, text),
        }
        if it.get("_jednostka_modelu"):
            item["poprawka"] = f"jednostka z tekstu ({jedn}) zamiast podanej przez model ({it['_jednostka_modelu']})"
        out.append(item)
    # ilości pominięte przez model – z tekstu, regułą (porównanie po ilości w jednostce bazowej)
    def covered(r, key):
        for x in out:
            if (x["ilosc_bazowa"], x["jednostka_bazowa"]) != key:
                continue
            sp = x.get("span")
            if x["element"] in (r["element"], "inne") or (sp and sp[0] < r["span"][1] and r["span"][0] < sp[1]):
                return True
        return False
    for r in rule_scope(text):
        bu, mult = BAZA.get(r["jednostka"], (r["jednostka"], 1.0))
        key = (round(r["ilosc"] * mult, 3), bu)
        if covered(r, key):
            continue
        out.append({"element": r["element"], "opis": r["element"], "ilosc": r["ilosc"], "jednostka": r["jednostka"],
                    "ilosc_bazowa": key[0], "jednostka_bazowa": bu, "parametry": r["parametry"], "cytat": r["cytat"],
                    "liczba_w_cytacie": True, "zrodlo": "dokladny", "span": r["span"], "z_reguly": True})
    return out


def scope_totals(items):
    """Sumy wg elementu i jednostki bazowej, np. {"sieć wodociągowa": {"m": 1200.0}}."""
    tot = {}
    for it in items:
        d = tot.setdefault(it["element"], {})
        d[it["jednostka_bazowa"]] = round(d.get(it["jednostka_bazowa"], 0) + it["ilosc_bazowa"], 3)
    return tot


def postprocess(raw, text, today):
    """Porządkuje odpowiedź modelu: normalizacja wartości, sprawdzenie cytatów, przeliczenia."""
    def g(obj, key, default=""):
        v = (obj or {}).get(key, default)
        return v if v is not None else default

    out = {}
    for key in ("zamawiajacy", "miejscowosc"):
        o = raw.get(key) or {}
        out[key] = {"wartosc": strip_address(str(g(o, "wartosc")).strip()), "cytat": str(g(o, "cytat")).strip()}

    o = raw.get("rodzaj_zamowienia") or {}
    rodzaj = norm_choice(g(o, "wartosc"), list(RODZAJE)) or ("roboty budowlane" if fold(g(o, "wartosc")).startswith("robot") else "")
    out["rodzaj_zamowienia"] = {"wartosc": rodzaj, "kod": RODZAJE.get(rodzaj, ""), "cytat": str(g(o, "cytat")).strip()}

    o = raw.get("wojewodztwo") or {}
    woj = norm_choice(g(o, "wartosc"), list(WOJEWODZTWA))
    out["wojewodztwo"] = {"wartosc": woj, "kod": WOJEWODZTWA.get(woj, ""), "cytat": str(g(o, "cytat")).strip(),
                          "wywnioskowane": bool(g(o, "wywnioskowane", False))}

    o = raw.get("planowana_kwota") or {}
    kwota = parse_amount(g(o, "kwota"))
    if kwota is None:
        kwota = parse_amount(g(o, "cytat"))
    waluta = (str(g(o, "waluta")).strip().upper() or ("PLN" if kwota is not None else ""))
    waluta = {"ZŁ": "PLN", "ZL": "PLN", "ZŁOTYCH": "PLN", "EURO": "EUR"}.get(waluta, waluta)
    vat = g(o, "vat", "nieokreślone")
    out["planowana_kwota"] = {"kwota": kwota, "waluta": waluta if kwota is not None else "",
                              "vat": vat if vat in ("netto", "brutto") else "nieokreślone",
                              "cytat": str(g(o, "cytat")).strip()}

    o = raw.get("planowany_czas") or {}
    out["planowany_czas"] = compute_time(
        liczba=parse_amount(g(o, "liczba")), jednostka=g(o, "jednostka"),
        podpisanie=parse_date(g(o, "data_podpisania")), termin=parse_date(g(o, "termin_do")),
        pierwotny=parse_date(g(o, "termin_pierwotny")), today=today)
    out["planowany_czas"]["cytat"] = str(g(o, "cytat")).strip()

    out["zakres_rzeczowy"] = norm_scope(raw.get("zakres_rzeczowy") or [], text)
    out["zakres_suma"] = scope_totals(out["zakres_rzeczowy"])

    kws, seen = [], set()
    for k in raw.get("slowa_kluczowe") or []:
        if isinstance(k, str):
            k = {"fraza": k, "kategoria": "inne"}
        fr = str((k or {}).get("fraza", "")).strip(" .,;")
        if not fr or fold(fr) in seen or looks_like_address(fr):
            continue
        seen.add(fold(fr))
        kat = k.get("kategoria") if k.get("kategoria") in KATEGORIE else "inne"
        span = find_span(fr, text)
        kws.append({"fraza": fr, "kategoria": kat, "w_tekscie": span is not None or check_quote(fr, text) != "brak",
                    "span": span})
    out["slowa_kluczowe"] = kws[:20]

    for key in ("zamawiajacy", "rodzaj_zamowienia", "miejscowosc", "wojewodztwo", "planowana_kwota", "planowany_czas"):
        c = out[key].get("cytat", "")
        out[key]["zrodlo"] = check_quote(c, text)
        out[key]["span"] = find_span(c, text)
    ground(out, text, today)
    return out


# ---- weryfikacja odpowiedzi z tekstem (zabezpieczenie przed pomyłkami i zmyśleniami modelu) --------------
_BAD_NAME = re.compile(r"\b(zł|zl|pln|mln|tys|netto|brutto|eur)\b|^\s*[\d.,\s/-]+\s*$|^(nieokreślon\w*|brak|nie dotyczy|nie podano|-)$", re.I)
_WOJ_STEMS = sorted(((fold(w)[:-3] if not w.startswith("śląs") else "slask", w) for w in WOJEWODZTWA), key=lambda x: -len(x[0]))


def _reject(field, reason):
    if field.get("wartosc"):
        field["odrzucone"] = {"wartosc": field["wartosc"], "powod": reason}
    field["wartosc"] = ""


def voivodeships_in_text(text):
    """Województwa wymienione wprost w tekście (dowolna forma: „podkarpackie”, „Województwa Podkarpackiego”, „woj. śl.”)."""
    t = fold(text)
    found, taken = {}, []
    for stem, name in _WOJ_STEMS:                       # najpierw dłuższe: „zachodniopomorsk” przed „pomorsk”
        for m in re.finditer(r"\b" + re.escape(stem) + r"\w*", t):
            if any(a <= m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            found.setdefault(name, []).append(m.start())
    return found


def ground(out, text, today):
    # zamawiający i miejscowość: to muszą być nazwy, które występują w tekście
    for key, label in (("zamawiajacy", "nazwę zamawiającego"), ("miejscowosc", "miejscowość")):
        f = out[key]
        v = f.get("wartosc", "")
        if not v:
            continue
        if _BAD_NAME.search(v):
            _reject(f, f"to nie wygląda na {label} (kwota, liczba albo „brak”)")
        elif find_span(v, text) is None:
            _reject(f, "nie występuje w tekście")
        else:
            f["zrodlo"] = f["zrodlo"] if f["zrodlo"] != "brak" else "czesciowy"
            f["span"] = f["span"] or find_span(v, text)
    if not out["zamawiajacy"].get("wartosc"):             # model nie podał albo odrzucony – spróbuj regułą
        v, pos = find_contracting_authority(text)
        if v:
            out["zamawiajacy"].update(wartosc=v, cytat=v, zrodlo="dokladny", z_reguly=True,
                                      span=[pos, pos + len(v)] if text[pos:pos + len(v)] == v else find_span(v, text))

    # województwo: wymienione w tekście ma pierwszeństwo przed odpowiedzią modelu
    w = out["wojewodztwo"]
    found = voivodeships_in_text(text)
    if found:
        best = max(found, key=lambda n: (len(found[n]), -found[n][0]))
        if w.get("wartosc") and w["wartosc"] != best and w["wartosc"] not in found:
            w["odrzucone"] = {"wartosc": w["wartosc"], "powod": f"w tekście jest województwo {best}"}
        w.update(wartosc=best, kod=WOJEWODZTWA[best], wywnioskowane=False, zrodlo="dokladny")
        pos = found[best][0]
        w["span"] = [pos, pos + len(best)] if fold(text[pos:pos + len(best)]) == fold(best) else find_span(best, text)
        w["cytat"] = text[w["span"][0]:w["span"][1]] if w.get("span") else best
    elif w.get("wartosc"):
        w.update(wywnioskowane=True, cytat="", span=None, zrodlo="brak")   # nie pada w tekście – tylko wniosek modelu

    # kwota musi wystąpić w tekście (z dokładnością do mnożnika „mln”/„tys.”)
    k = out["planowana_kwota"]
    if k.get("kwota") is not None and not any(abs(k["kwota"] - a) < 0.6 for a in amounts_in_text(text)):
        k["odrzucone"] = {"wartosc": k["kwota"], "powod": "tej kwoty nie ma w tekście"}
        k.update(kwota=None, waluta="")

    # czas: daty muszą być w tekście, liczba okresu też; datę podpisania można znaleźć samemu
    c = out["planowany_czas"]
    odrz = []
    vals = {key: parse_date(c.get(key)) for key in ("data_podpisania", "termin_do", "termin_pierwotny")}
    for key, d in vals.items():
        if d and not date_in_text(d, text):
            odrz.append(f"{key} {d.isoformat()} – brak tej daty w tekście")
            vals[key] = None
    z_reguly = []
    if not vals["data_podpisania"]:                       # „Umowa nr 272.4.27.1.0.2025 z dnia 14.02.2025 r.”, także w nowej linii
        m = re.search(r"umow\w*[^;]{0,100}?z\s+dnia\s*:?\s*" + _DATE_PAT, text, re.I)
        if m and parse_date(m.group(1)):
            vals["data_podpisania"] = parse_date(m.group(1)); z_reguly.append("data_podpisania")
    if not vals["termin_pierwotny"]:                      # „Dotychczasowy termin zakończenia …: 30.10.2025 r.”
        m = re.search(r"(?:dotychczasow|pierwotn|obecn|umown)\w*\s+termin\w*[^:\n]{0,80}?:?\s*" + _DATE_PAT, text, re.I)
        if m and parse_date(m.group(1)):
            vals["termin_pierwotny"] = parse_date(m.group(1)); z_reguly.append("termin_pierwotny")
    if vals["termin_pierwotny"] and (not vals["termin_do"] or vals["termin_do"] == vals["termin_pierwotny"]):
        m = re.search(r"(?:nowy|zmienion\w*|wydłużon\w*)\s+termin\w*[^:\n]{0,80}?:?\s*" + _DATE_PAT
                      + r"|przedłuż\w*[^.\n]{0,60}?\bdo\s+(?:dnia\s+)?" + _DATE_PAT, text, re.I)
        if m and parse_date(m.group(1) or m.group(3)):
            vals["termin_do"] = parse_date(m.group(1) or m.group(3)); z_reguly.append("termin_do")
    liczba, jedn = c.get("liczba"), c.get("jednostka")
    if liczba is not None and not duration_in_text(liczba, jedn, text):
        odrz.append(f"czas trwania {liczba:g} {jedn} – brak tej liczby w tekście")
        liczba, jedn = None, ""
    cyt = c.get("cytat", "")
    new = compute_time(liczba=liczba, jednostka=jedn, podpisanie=vals["data_podpisania"], termin=vals["termin_do"],
                       pierwotny=vals["termin_pierwotny"], today=today)
    new.update(cytat=cyt, zrodlo=c.get("zrodlo"), span=c.get("span"))
    if z_reguly:
        new["z_reguly"] = z_reguly                        # pola uzupełnione regułą z tekstu, nie przez model
    if odrz:
        new["odrzucone"] = {"wartosc": "; ".join(odrz), "powod": "model podał wartości, których nie ma w tekście"}
    out["planowany_czas"] = new

    # słowa kluczowe: odrzuć te, których nie ma w tekście ani w żadnej formie (najczęściej przepisane z instrukcji)
    out["slowa_kluczowe"] = [k2 for k2 in out["slowa_kluczowe"] if k2.get("w_tekscie")] or out["slowa_kluczowe"]


# ---- odczyt plików PDF / DOCX / ODT -------------------------------------------------------
class FileError(Exception):
    """Błąd odczytu pliku z komunikatem dla użytkownika."""


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_part(xml_bytes):
    """Tekst jednej części dokumentu Word: akapity, tabulatory, podziały, tabele (komórki rozdzielone „ | ”)."""
    root = ET.fromstring(xml_bytes)
    out = []

    def para_text(p):
        buf = []
        for el in p.iter():
            if el.tag == W + "t":
                buf.append(el.text or "")
            elif el.tag == W + "tab":
                buf.append("\t")
            elif el.tag in (W + "br", W + "cr"):
                buf.append("\n")
            elif el.tag == W + "noBreakHyphen":
                buf.append("-")
        return "".join(buf)

    def walk(node):
        for child in node:
            if child.tag == W + "p":
                out.append(para_text(child))
            elif child.tag == W + "tbl":
                for tr in child.iter(W + "tr"):
                    cells = []
                    for tc in tr.findall(W + "tc"):
                        cell = " ".join(t for t in (para_text(p).strip() for p in tc.iter(W + "p")) if t)
                        cells.append(re.sub(r"\s*\n\s*", " / ", cell))   # jeden wiersz tabeli = jedna linia
                    if any(cells):
                        out.append(" | ".join(cells))
                out.append("")
            elif child.tag in (W + "body", W + "sdt", W + "sdtContent", W + "customXml"):
                walk(child)

    walk(root)
    return "\n".join(out)


def extract_docx(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise FileError("To nie jest poprawny plik .docx (uszkodzony albo stary format .doc – zapisz go w Wordzie jako .docx).")
    names = zf.namelist()
    if "word/document.xml" not in names:
        raise FileError("W pliku nie ma treści dokumentu Word.")
    parts = [_docx_part(zf.read("word/document.xml"))]
    for extra in ("word/footnotes.xml", "word/endnotes.xml"):   # przypisy bywają ważne (kwoty, terminy)
        if extra in names:
            t = _docx_part(zf.read(extra)).strip()
            if t:
                parts.append("Przypisy:\n" + t)
    return "\n\n".join(parts), {}


def extract_odt(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        root = ET.fromstring(zf.read("content.xml"))
    except Exception:
        raise FileError("To nie jest poprawny plik .odt.")
    T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    TB = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    out = []

    def text_of(el):
        buf = [el.text or ""]
        for c in el:
            if c.tag == T + "s":
                buf.append(" " * int(c.get(T + "c", "1")))
            elif c.tag == T + "tab":
                buf.append("\t")
            elif c.tag == T + "line-break":
                buf.append("\n")
            else:
                buf.append(text_of(c))
            buf.append(c.tail or "")
        return "".join(buf)

    def walk(node):
        for c in node:
            if c.tag in (T + "p", T + "h"):
                out.append(text_of(c))
            elif c.tag == TB + "table-row":
                out.append(" | ".join(" ".join(text_of(p).strip() for p in cell.iter() if p.tag in (T + "p", T + "h")).strip()
                                      for cell in c if cell.tag == TB + "table-cell"))
            else:
                walk(c)

    walk(root)
    return "\n".join(out), {}


# ---- OCR (Tesseract) dla PDF bez warstwy tekstowej ------------------------------------------------
_ocr_cache = {}


def find_tesseract():
    """Ścieżka do programu tesseract: --tesseract, PATH, typowe miejsca instalacji w Windows."""
    if "exe" in _ocr_cache:
        return _ocr_cache["exe"]
    cands = [TESSERACT, shutil.which("tesseract")]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, "Tesseract-OCR", "tesseract.exe"),
                      os.path.join(base, "Programs", "Tesseract-OCR", "tesseract.exe")]
    exe = next((c for c in cands if c and os.path.isfile(c)), None)
    _ocr_cache["exe"] = exe
    return exe


def ocr_status():
    """Czy OCR jest dostępne i z jakimi językami (do /api/status i komunikatu przy starcie)."""
    if "status" in _ocr_cache:
        return _ocr_cache["status"]
    exe = find_tesseract()
    st = {"dostepne": False, "program": exe, "jezyki": [], "jezyk": OCR_LANG}
    if exe:
        try:
            out = subprocess.run([exe, "--list-langs"], capture_output=True, timeout=20)
            langs = [l.strip() for l in out.stdout.decode("utf-8", "replace").splitlines()[1:] if l.strip()]
            st["jezyki"] = langs
            need = [l for l in OCR_LANG.split("+") if l]
            st["dostepne"] = all(l in langs for l in need)
            if not st["dostepne"]:
                st["brak_jezyka"] = [l for l in need if l not in langs]
        except Exception as e:
            st["blad"] = str(e)
    _ocr_cache["status"] = st
    return st


def _page_images(data, max_pages):
    """Obrazy stron do OCR: renderowanie pdftoppm (jeśli jest) albo obrazy osadzone w PDF (typowy skan)."""
    exe = shutil.which("pdftoppm")
    if exe:
        tmp = tempfile.mkdtemp()
        try:
            src = Path(tmp) / "plik.pdf"
            src.write_bytes(data)
            subprocess.run([exe, "-r", "300", "-gray", "-png", "-l", str(max_pages), str(src), str(Path(tmp) / "s")],
                           capture_output=True, timeout=300, check=True)
            return [p.read_bytes() for p in sorted(Path(tmp).glob("s*.png"))]
        except Exception:
            pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(data))
    pages = []
    for page in list(reader.pages)[:max_pages]:
        try:
            imgs = list(page.images)
        except Exception:
            imgs = []
        if not imgs:
            continue
        best = max(imgs, key=lambda im: len(im.data))      # skan = jeden duży obraz na stronę
        pages.append(_prepare_image(best.data))
    return pages


def _prepare_image(raw):
    """Jeśli jest Pillow: odcienie szarości i powiększenie małych skanów (~300 dpi) – lepsze rozpoznawanie."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im = im.convert("L")
        if im.width < 2400:
            f = 2480 / im.width
            im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return raw            # bez Pillow – oryginalne dane (JPEG/PNG Tesseract czyta sam)


def ocr_pdf(data):
    st = ocr_status()
    if not st["dostepne"]:
        if st["program"]:
            raise FileError(f"Ten PDF to obraz strony (skan) – potrzebne jest OCR. Tesseract jest zainstalowany, ale bez języka "
                            f"„{'+'.join(st.get('brak_jezyka') or [OCR_LANG])}”: doinstaluj dane języka polskiego (pol.traineddata). "
                            f"Na razie możesz skopiować tekst z przeglądarki PDF i wkleić go do pola.")
        raise FileError("Ten PDF to obraz strony (skan albo zdjęcie) – nie ma w nim tekstu, a przeglądarka PDF rozpoznaje go sama – "
                        "dlatego da się go zaznaczyć. Żeby aplikacja też go odczytała, zainstaluj Tesseract OCR z językiem polskim "
                        "(Windows: github.com/UB-Mannheim/tesseract/wiki – przy instalacji zaznacz „Polish”) i uruchom serwer ponownie. "
                        "Na razie możesz skopiować tekst z przeglądarki PDF i wkleić go do pola.")
    images = _page_images(data, OCR_MAX_PAGES)
    if not images:
        raise FileError("W tym PDF nie ma ani tekstu, ani obrazów stron do rozpoznania.")
    texts = []
    for img in images:
        res = subprocess.run([st["program"], "stdin", "stdout", "-l", OCR_LANG, "--psm", "3"],
                             input=img, capture_output=True, timeout=180)
        if res.returncode != 0:
            raise FileError("OCR nie powiódł się: " + res.stderr.decode("utf-8", "replace")[:200])
        texts.append(res.stdout.decode("utf-8", "replace"))
    return "\n\n".join(texts), len(images)


def extract_pdf(data):
    pages = None
    text = None
    try:
        import pypdf  # opcjonalny pakiet: pip install pypdf
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    raise FileError("PDF jest zabezpieczony hasłem.")
            pages = len(reader.pages)
            chunks = []
            for page in reader.pages:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception:
                    chunks.append("")
            text = "\n\n".join(chunks)
        except FileError:
            raise
        except Exception as e:
            raise FileError(f"Nie udało się odczytać PDF: {type(e).__name__}: {e}")
    except ImportError:
        exe = shutil.which("pdftotext")
        if not exe:
            raise FileError("Do odczytu PDF potrzebny jest pakiet pypdf. Zainstaluj go: pip install pypdf "
                            "(albo uruchom aplikację przez: uv run serwer.py) i uruchom serwer ponownie.")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "plik.pdf"
            src.write_bytes(data)
            res = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(src), "-"], capture_output=True, timeout=120)
            if res.returncode != 0:
                raise FileError("pdftotext nie odczytał pliku: " + res.stderr.decode("utf-8", "replace")[:200])
            text = res.stdout.decode("utf-8", "replace")
            pages = text.count("\f") + 1
            text = text.replace("\f", "\n\n")
    # pypdf bywa bezradny wobec czcionek bez mapowania na Unicode – wtedy spróbuj pdftotext (poppler)
    if text and garbled_ratio(text) > 0.1 and shutil.which("pdftotext"):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "plik.pdf"
                src.write_bytes(data)
                res = subprocess.run([shutil.which("pdftotext"), "-layout", "-enc", "UTF-8", str(src), "-"],
                                     capture_output=True, timeout=120)
            alt = res.stdout.decode("utf-8", "replace").replace("\f", "\n\n")
            if res.returncode == 0 and garbled_ratio(alt) < garbled_ratio(text):
                text = alt
        except Exception:
            pass
    no_text = len(re.sub(r"\s", "", text or "")) < 30
    garbled = not no_text and garbled_ratio(text) > 0.1
    if no_text or garbled:
        # brak warstwy tekstowej (skan) albo krzaki – spróbuj OCR, jeśli jest Tesseract
        st = ocr_status()
        if st["dostepne"] or no_text:
            ocr_text, n = ocr_pdf(data)        # bez Tesseracta: FileError z instrukcją instalacji
            if len(re.sub(r"\s", "", ocr_text)) >= 30:
                info = {"strony": pages, "ocr": True}
                if pages and pages > OCR_MAX_PAGES:
                    info["ocr_strony"] = n
                return ocr_text, info
            raise FileError("OCR nie znalazł w tym PDF czytelnego tekstu.")
    if garbled:
        raise FileError("Tekst odczytany z tego PDF jest nieczytelny (krzaki) – plik używa czcionek bez mapowania "
                        "na litery Unicode albo jest skanem. Spróbuj: otwórz PDF, zaznacz i skopiuj tekst do pola "
                        "(jeśli kopia też jest nieczytelna, potrzebne jest OCR), albo wyeksportuj dokument ponownie do PDF/DOCX.")
    if len(re.sub(r"\s", "", text or "")) < 30:
        raise FileError("W tym PDF nie ma warstwy tekstowej – to prawdopodobnie skan. Potrzebne byłoby OCR "
                        "(rozpoznawanie tekstu), którego aplikacja na razie nie ma.")
    return text, {"strony": pages}


_ODD = re.compile(r"[\uE000-\uF8FF\uFFFD\x00-\x08\x0B\x0C\x0E-\x1F]|\(cid:\d+\)|/g\d{2,}|/uni[0-9A-F]{4}")
_OK = re.compile(r"[\w\s.,;:!?()\[\]{}\"'„”«»\-–—/\\%&*+=<>#@§|°€$~^`]", re.U)


def garbled_ratio(text):
    """Udział „dziwnych” znaków: 0 = zwykły tekst, >0.1 = najpewniej krzaki (czcionki bez mapowania Unicode)."""
    sample = (text or "")[:20000]
    if not sample.strip():
        return 1.0
    odd = sum(len(m.group(0)) for m in _ODD.finditer(sample))
    letters = sum(1 for ch in sample if _OK.match(ch))
    return max(odd / len(sample), 1 - letters / len(sample))


def clean_extracted(text):
    """Porządkuje tekst z pliku: łączy wyrazy przeniesione z dywizem, usuwa nadmiar odstępów."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00ad", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)            # „kanaliza-\ncji” → „kanalizacji”
    text = re.sub(r"[ \t\u00a0]+\n", "\n", text)
    text = re.sub(r"[ \u00a0]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


EXTRACTORS = {".docx": extract_docx, ".odt": extract_odt, ".pdf": extract_pdf}


def extract_file(name, data):
    ext = Path(name).suffix.lower()
    if ext == ".doc":
        raise FileError("Stary format Word (.doc) nie jest obsługiwany – zapisz plik jako .docx.")
    if ext not in EXTRACTORS:
        raise FileError(f"Nieobsługiwany typ pliku: {ext or 'brak rozszerzenia'}. Obsługiwane: PDF, DOCX, ODT, TXT, MD, CSV, HTML.")
    text, info = EXTRACTORS[ext](data)
    return clean_extracted(text), info


# ---- komunikacja z modelem --------------------------------------------------------------
def http_json(url, payload=None, timeout=LLM_TIMEOUT_S):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_open(url, payload, timeout=LLM_TIMEOUT_S):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def parse_model_json(content):
    content = (content or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class Cancelled(Exception):
    """Analizę przerwano (przycisk „Przerwij” albo zamknięta karta)."""


class OutputTooLong(Exception):
    """Model doszedł do limitu długości odpowiedzi (MAX_TOKENS) przed końcem JSON."""


_ctx_cache = {}


def model_max_ctx(cfg):
    """Maksymalne okno kontekstu modelu wg Ollamy (/api/show), None gdy nie wiadomo."""
    key = (cfg["url"], cfg["model"])
    if key in _ctx_cache:
        return _ctx_cache[key]
    val = None
    if cfg["backend"] == "ollama":
        try:
            info = http_json(cfg["url"].rstrip("/") + "/api/show", {"model": cfg["model"]}, timeout=10).get("model_info") or {}
            vals = [v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int)]
            val = vals[0] if vals else None
        except Exception:
            return None            # nie zapamiętuj – może model jeszcze nie jest pobrany
    _ctx_cache[key] = val
    return val


def effective_ctx(cfg):
    m = model_max_ctx(cfg)
    return min(cfg["ctx"], m) if m else cfg["ctx"]


def text_budget(cfg):
    """Ile znaków tekstu zmieści się w oknie kontekstu obok instrukcji i odpowiedzi."""
    tokens = effective_ctx(cfg) - cfg["max_tokens"] - PROMPT_RESERVE_TOKENS
    return max(2000, min(MAX_TEXT_CHARS, int(tokens * CHARS_PER_TOKEN)))


def _stream_lines(resp, should_stop):
    """Czyta odpowiedź strumieniową linia po linii; przerywa, gdy should_stop() – zamknięcie połączenia
    sprawia, że Ollama / llama.cpp przestają generować."""
    for raw_line in resp:
        if should_stop():
            resp.close()
            raise Cancelled()
        line = raw_line.decode("utf-8", "replace").strip()
        if line:
            yield line


def call_llm(cfg, text, today, trace=None, should_stop=lambda: False):
    """Wysyła tekst do modelu (strumieniowo, z możliwością przerwania).
    `trace` (słownik) dostaje zapytanie i odpowiedź – do zapisu debug."""
    trace = trace if trace is not None else {}
    system = SYSTEM_PROMPT.format(today=today.isoformat(), woj=", ".join(WOJEWODZTWA))
    user = ("Tekst do analizy:\n\"\"\"\n" + text + "\n\"\"\"\n\n"
            "Zwróć JSON z polami: zamawiajacy, rodzaj_zamowienia, miejscowosc, wojewodztwo, "
            "planowana_kwota, planowany_czas, slowa_kluczowe.")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    base = cfg["url"].rstrip("/")
    parts = []
    if cfg["backend"] == "ollama":
        body = {"model": cfg["model"], "messages": messages, "stream": True, "format": SCHEMA,
                "keep_alive": cfg["keep_alive"],
                "options": {"temperature": TEMPERATURE, "num_ctx": effective_ctx(cfg), "num_predict": cfg["max_tokens"]}}
        trace["request"] = {"url": f"{base}/api/chat", "body": body}
        final = {}
        with http_open(f"{base}/api/chat", body) as resp:
            for line in _stream_lines(resp, should_stop):
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                parts.append((chunk.get("message") or {}).get("content", ""))
                if chunk.get("done"):
                    final = chunk
        final.pop("message", None)
        trace["response"] = final
        stats = {"tokens_in": final.get("prompt_eval_count"), "tokens_out": final.get("eval_count")}
        truncated = final.get("done_reason") == "length"
    else:
        body = {"model": cfg["model"], "messages": messages, "temperature": TEMPERATURE, "stream": True,
                "max_tokens": cfg["max_tokens"], "stream_options": {"include_usage": True},
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "inwestycja", "schema": SCHEMA, "strict": True}}}
        url = f"{base}/v1/chat/completions"
        trace["request"] = {"url": url, "body": body}
        try:
            resp = http_open(url, body)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 422):
                raise
            trace["json_schema_rejected"] = f"HTTP {e.code}: " + e.read().decode("utf-8", "replace")[:500]
            body = dict(body, response_format={"type": "json_object"})   # starsze serwery bez json_schema
            body.pop("stream_options", None)
            trace["request"] = {"url": url, "body": body}
            resp = http_open(url, body)
        finish, usage = None, {}
        with resp:
            for line in _stream_lines(resp, should_stop):
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                usage = chunk.get("usage") or usage
                for ch in chunk.get("choices") or []:
                    parts.append((ch.get("delta") or {}).get("content") or "")
                    finish = ch.get("finish_reason") or finish
        trace["response"] = {"finish_reason": finish, "usage": usage}
        stats = {"tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens")}
        truncated = finish == "length"
    content = "".join(parts)
    trace["content"] = content
    if truncated:
        try:
            return parse_model_json(content), content, stats
        except json.JSONDecodeError:
            raise OutputTooLong()
    return parse_model_json(content), content, stats


def llm_status(cfg):
    base = cfg["url"].rstrip("/")
    out = {"backend": cfg["backend"], "url": base, "model": cfg["model"], "online": False, "model_ready": False, "models": []}
    try:
        if cfg["backend"] == "ollama":
            names = [m.get("name", "") for m in http_json(f"{base}/api/tags", timeout=5).get("models", [])]
        else:
            names = [m.get("id", "") for m in http_json(f"{base}/v1/models", timeout=5).get("data", [])]
        out["online"] = True
        out["models"] = names
        want = cfg["model"].lower()
        out["model_ready"] = any(n.lower() == want or n.lower() == want + ":latest" for n in names) \
            or (cfg["backend"] == "openai" and len(names) > 0)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["ocr"] = ocr_status()["dostepne"]
    if out["model_ready"]:
        out["ctx"] = effective_ctx(cfg)
        out["model_max_ctx"] = model_max_ctx(cfg)
        out["max_tokens"] = cfg["max_tokens"]
        out["max_znakow"] = text_budget(cfg)
    return out


# ---- zapis debug -------------------------------------------------------------------------
_debug_lock = threading.Lock()


def save_debug(debug_dir, record):
    """Zapisuje pełny przebieg analizy do debug/RRRRMMDD-HHMMSS-mmm_model.json. Zwraca ścieżkę lub None."""
    if not debug_dir:
        return None
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now()
        model = re.sub(r"[^\w.-]+", "_", record.get("model", "model"))[:60]
        with _debug_lock:
            path = debug_dir / f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}_{model}.json"
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    except Exception as e:
        sys.stderr.write(f"Nie udało się zapisać pliku debug: {e}\n")
        return None


def build_result_file(result, text, meta):
    """Czysty wynik analizy – ten sam układ co „Pobierz JSON” na stronie."""
    r = result
    k, c = r["planowana_kwota"], r["planowany_czas"]
    return {
        "zamawiajacy": r["zamawiajacy"]["wartosc"] or None,
        "rodzaj_zamowienia": r["rodzaj_zamowienia"]["wartosc"] or None,
        "rodzaj_zamowienia_kod": r["rodzaj_zamowienia"].get("kod") or None,
        "miejscowosc": r["miejscowosc"]["wartosc"] or None,
        "wojewodztwo": r["wojewodztwo"]["wartosc"] or None,
        "wojewodztwo_kod": r["wojewodztwo"].get("kod") or None,
        "wojewodztwo_wywnioskowane": bool(r["wojewodztwo"].get("wywnioskowane")),
        "planowana_kwota": {"kwota": k["kwota"], "waluta": k["waluta"] or "PLN", "vat": k["vat"]} if k["kwota"] is not None else None,
        "planowany_czas": ({"liczba": c["liczba"], "jednostka": c["jednostka"] or None,
                            "data_podpisania": c["data_podpisania"] or None,
                            "termin_do": c["termin_do"] or None, "termin_wyliczony": c["termin_wyliczony"] or None,
                            "termin_pierwotny": c["termin_pierwotny"] or None, "przedluzenie_dni": c["przedluzenie_dni"],
                            "dni": c["dni"], "podstawa": c["podstawa"] or None, "uwaga": c["uwaga"] or None}
                           if (c["liczba"] is not None or c["termin_do"] or c["data_podpisania"]) else None),
        "zakres_rzeczowy": [{k2: x[k2] for k2 in ("element", "opis", "ilosc", "jednostka", "ilosc_bazowa",
                                                  "jednostka_bazowa", "parametry", "cytat")} for x in r.get("zakres_rzeczowy", [])],
        "zakres_suma": r.get("zakres_suma", {}),
        "slowa_kluczowe": [{"fraza": x["fraza"], "kategoria": x["kategoria"]} for x in r["slowa_kluczowe"]],
        "cytaty": {key: r[key].get("cytat") or None for key in
                   ("zamawiajacy", "rodzaj_zamowienia", "miejscowosc", "wojewodztwo", "planowana_kwota", "planowany_czas")},
        "model": meta["model"],
        "data_analizy": meta["data"],
        "czas_analizy_s": meta["czas_s"],
        "tekst": text,
    }


# Kolumny zestawienia do porównywania projektów – stałe, niezależne od treści dokumentu.
def _summary_columns():
    cols = ["data_analizy", "plik_wyniku", "zamawiajacy", "miejscowosc", "wojewodztwo", "rodzaj_zamowienia",
            "kwota", "waluta", "vat", "data_podpisania", "termin_do", "termin_pierwotny", "przedluzenie_dni",
            "czas_realizacji_dni", "podstawa_czasu"]
    for el, units in ELEMENTY.items():
        cols += [f"{el} [{u}]" for u in units]
    return cols + ["inne pozycje zakresu (liczba)", "slowa_kluczowe", "model"]


def _num(v):
    if v is None or v == "":
        return ""
    v = round(float(v), 3)
    return (str(int(v)) if v == int(v) else str(v)).replace(".", ",")      # przecinek – Excel PL


def append_summary(results_dir, data, result_path):
    """Dopisuje wiersz do wyniki/zestawienie.csv (separator ;, UTF-8 z BOM – otwiera się w Excelu)."""
    cols = _summary_columns()
    path = results_dir / "zestawienie.csv"
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as f:
            head = next(csv.reader(f, delimiter=";"), [])
        if head != cols:                                   # inny układ kolumn (starsza wersja) – nowy plik
            path = results_dir / f"zestawienie_{VERSION}.csv"
    k, c = data.get("planowana_kwota") or {}, data.get("planowany_czas") or {}
    row = {"data_analizy": data["data_analizy"], "plik_wyniku": result_path.name if result_path else "",
           "zamawiajacy": data.get("zamawiajacy") or "", "miejscowosc": data.get("miejscowosc") or "",
           "wojewodztwo": data.get("wojewodztwo") or "", "rodzaj_zamowienia": data.get("rodzaj_zamowienia") or "",
           "kwota": _num(k.get("kwota")), "waluta": k.get("waluta") or "", "vat": k.get("vat") or "",
           "data_podpisania": c.get("data_podpisania") or "", "termin_do": c.get("termin_do") or c.get("termin_wyliczony") or "",
           "termin_pierwotny": c.get("termin_pierwotny") or "", "przedluzenie_dni": _num(c.get("przedluzenie_dni")),
           "czas_realizacji_dni": _num(c.get("dni")), "podstawa_czasu": c.get("podstawa") or "",
           "slowa_kluczowe": "; ".join(x["fraza"] for x in data.get("slowa_kluczowe", [])), "model": data.get("model") or ""}
    inne = 0
    for el, units in (data.get("zakres_suma") or {}).items():
        for u, v in units.items():
            col = f"{el} [{u}]"
            if col in cols:
                row[col] = _num(v)
            else:
                inne += 1
    row["inne pozycje zakresu (liczba)"] = _num(inne) if inne else ""
    new = not path.exists()
    with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", restval="")
        if new:
            w.writeheader()
        w.writerow(row)
    return path


def save_result(results_dir, data):
    """Zapisuje wynik do wyniki/RRRRMMDD-GGMMSS_zamawiajacy.json. Zwraca ścieżkę lub None."""
    if not results_dir:
        return None
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now()
        who = fold(data.get("zamawiajacy") or data.get("miejscowosc") or "inwestycja")
        who = re.sub(r"[^a-z0-9]+", "_", who).strip("_")[:40] or "inwestycja"
        with _debug_lock:
            path = results_dir / f"{now:%Y%m%d-%H%M%S}_{who}.json"
            i = 2
            while path.exists():
                path = results_dir / f"{now:%Y%m%d-%H%M%S}_{who}_{i}.json"
                i += 1
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                append_summary(results_dir, data, path)
            except Exception as e:
                sys.stderr.write(f"Nie udało się dopisać do zestawienia: {e}\n")
        return path
    except Exception as e:
        sys.stderr.write(f"Nie udało się zapisać wyniku: {e}\n")
        return None


def rel_path(path):
    if not path:
        return None
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# ---- serwer HTTP --------------------------------------------------------------------------
ACTIVE = {}                      # id analizy -> threading.Event (przerwanie)
_active_lock = threading.Lock()


def client_gone(sock):
    """Czy przeglądarka zamknęła połączenie (przerwane fetch, zamknięta karta)."""
    try:
        r, _, _ = select.select([sock], [], [], 0)
        if r:
            return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True
    return False


class Handler(http.server.SimpleHTTPRequestHandler):
    cfg = {}
    debug_dir = DEBUG_DIR
    results_dir = RESULTS_DIR

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def end_headers(self):
        # strona i skrypty zawsze świeże – inaczej przeglądarka potrafi trzymać starą wersję app.js
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                     # przeglądarka już zamknęła połączenie (np. po „Przerwij”)

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/api/status":
            return self._json(200, dict(llm_status(self.cfg), wersja=VERSION))
        if path == "/":
            self.path = "/index.html"
            path = "/index.html"
        if ".." in path or not (path in STATIC_FILES or path.startswith(STATIC_DIRS)):
            return self.send_error(404, "Nie znaleziono")
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/file":
            return self._file()
        if path == "/api/cancel":
            try:
                rid = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}").get("id", "")
            except Exception:
                rid = ""
            with _active_lock:
                ev = ACTIVE.get(rid)
            if ev:
                ev.set()
            return self._json(200, {"przerwano": bool(ev)})
        if path != "/api/extract":
            return self._json(404, {"title": "Nieznana ścieżka"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4_000_000:
                return self._json(413, {"title": "Za duży tekst"})
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"title": "Niepoprawne zapytanie"})
        text = str(payload.get("text", "")).strip()
        if len(text) < 20:
            return self._json(400, {"title": "Wpisz dłuższy opis inwestycji (co najmniej kilka zdań)."})
        warnings = []
        budget = text_budget(self.cfg)
        if len(text) > budget:
            text = text[:budget]
            warnings.append(f"Tekst przycięto do {budget:,} znaków – tyle mieści okno kontekstu modelu "
                            f"({effective_ctx(self.cfg):,} tokenów). Wklej najważniejszy fragment albo uruchom serwer "
                            f"z większym --ctx (jeśli model na to pozwala).".replace(",", " "))
        rid = (self.headers.get("X-Request-Id") or uuid.uuid4().hex)[:64]
        stop_ev = threading.Event()
        with _active_lock:
            ACTIVE[rid] = stop_ev
        try:
            return self._analyze(text, warnings, stop_ev)
        finally:
            with _active_lock:
                ACTIVE.pop(rid, None)

    def _analyze(self, text, warnings, stop_ev):
        today = dt.date.today()
        t0 = time.time()
        trace = {}
        record = {"czas_zapytania": dt.datetime.now().isoformat(timespec="seconds"),
                  "model": self.cfg["model"], "backend": self.cfg["backend"], "llm_url": self.cfg["url"],
                  "tekst": text, "ostrzezenia": warnings, "trace": trace}

        def fail(status, payload, exc=None):
            record.update(status="blad", http_status=status, blad=payload,
                          czas_s=round(time.time() - t0, 2))
            if exc is not None:
                record["wyjatek"] = f"{type(exc).__name__}: {exc}"
            path = save_debug(self.debug_dir, record)
            if path:
                payload = dict(payload, debug_plik=rel_path(path),
                               surowa_tresc=trace.get("content"))
            return self._json(status, payload)

        def should_stop():
            return stop_ev.is_set() or client_gone(self.connection)

        try:
            raw, content, stats = call_llm(self.cfg, text, today, trace, should_stop)
        except Cancelled as e:
            sys.stderr.write("Analizę przerwano – model zatrzymany.\n")
            return fail(499, {"title": "Analizę przerwano."}, e)
        except OutputTooLong as e:
            return fail(502, {"title": f"Model doszedł do limitu długości odpowiedzi ({self.cfg['max_tokens']} tokenów) "
                                       f"i nie dokończył wyniku – możliwe, że się zapętlił. Spróbuj ponownie, skróć tekst "
                                       f"albo uruchom serwer z większym --max-tokens."}, e)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            hint = ""
            if e.code == 404 and "not found" in detail.lower():
                hint = f" Pobierz model: ollama pull {self.cfg['model']}"
            return fail(502, {"title": f"Serwer modelu zwrócił błąd HTTP {e.code}.{hint}", "detail": detail}, e)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            return fail(503, {"title": "Brak połączenia z lokalnym modelem. Czy Ollama (lub inny serwer modelu) działa?",
                              "detail": str(e)}, e)
        except json.JSONDecodeError as e:
            return fail(502, {"title": "Model zwrócił odpowiedź, której nie da się odczytać jako JSON.", "detail": str(e)}, e)
        except Exception as e:
            return fail(500, {"title": "Błąd analizy", "detail": f"{type(e).__name__}: {e}"}, e)
        if should_stop():          # model skończył, ale przeglądarka już zrezygnowała
            return fail(499, {"title": "Analizę przerwano."})
        result = postprocess(raw if isinstance(raw, dict) else {}, text, today)
        elapsed = round(time.time() - t0, 1)
        record.update(status="ok", czas_s=elapsed, statystyki=stats,
                      odpowiedz_sparsowana=raw, wynik_po_obrobce=result)
        path = save_debug(self.debug_dir, record)
        result_path = save_result(self.results_dir, build_result_file(
            result, text, {"model": self.cfg["model"], "data": dt.datetime.now().isoformat(timespec="seconds"), "czas_s": elapsed}))
        return self._json(200, {
            "wynik": result, "surowa_odpowiedz": raw, "surowa_tresc": content,
            "model": self.cfg["model"], "backend": self.cfg["backend"],
            "czas_s": elapsed, "statystyki": stats, "ostrzezenia": warnings,
            "dlugosc_tekstu": len(text),
            "debug_plik": rel_path(path),
            "wynik_plik": rel_path(result_path),
        })

    def _file(self):
        """POST /api/file – treść pliku w body, nazwa w nagłówku X-Filename (zakodowana jak w URL). Zwraca tekst."""
        name = urllib.parse.unquote(self.headers.get("X-Filename", "plik"))
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._json(400, {"title": "Pusty plik."})
        if length > MAX_FILE_BYTES:
            return self._json(413, {"title": f"Plik jest za duży (maks. {MAX_FILE_BYTES // 1024 // 1024} MB)."})
        data = self.rfile.read(length)
        t0 = time.time()
        try:
            text, info = extract_file(name, data)
        except FileError as e:
            return self._json(422, {"title": str(e)})
        except Exception as e:
            return self._json(500, {"title": "Nie udało się odczytać pliku.", "detail": f"{type(e).__name__}: {e}"})
        warnings = []
        budget = text_budget(self.cfg)
        if len(text) > budget:
            warnings.append(f"Plik ma {len(text):,} znaków, a model zmieści około {budget:,} – do analizy trafi tylko "
                            f"początek. Usuń z pola mniej ważne fragmenty przed analizą.".replace(",", " "))
        return self._json(200, {"tekst": text, "nazwa": name, "znaki": len(text), "czas_s": round(time.time() - t0, 2),
                                "ostrzezenia": warnings, **info})

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global TESSERACT, OCR_LANG
    ap = argparse.ArgumentParser(description="Smart Kraken – analiza opisu inwestycji lokalnym LLM")
    ap.add_argument("--backend", choices=["ollama", "openai"], default=BACKEND,
                    help="rodzaj serwera modelu: ollama (domyślnie) albo openai (llama.cpp, LM Studio, vLLM…)")
    ap.add_argument("--llm-url", default=None, help=f"adres serwera modelu (domyślnie {LLM_URL} dla ollama, http://127.0.0.1:8080 dla openai)")
    ap.add_argument("--model", default=MODEL, help=f"nazwa modelu (domyślnie {MODEL})")
    ap.add_argument("--ctx", type=int, default=CTX, help=f"okno kontekstu w tokenach (domyślnie {CTX}; najwyżej tyle, ile ma model)")
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS, help=f"limit długości odpowiedzi modelu (domyślnie {MAX_TOKENS})")
    ap.add_argument("--keep-alive", default=KEEP_ALIVE, help=f"jak długo Ollama trzyma model w pamięci (domyślnie {KEEP_ALIVE}; -1 = zawsze)")
    ap.add_argument("--tesseract", default=None, help="ścieżka do tesseract.exe, jeśli nie ma go w PATH ani w Program Files")
    ap.add_argument("--ocr-lang", default=OCR_LANG, help=f"język OCR (domyślnie {OCR_LANG}; np. pol+eng)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="nie otwieraj przeglądarki")
    ap.add_argument("--debug-dir", default=str(DEBUG_DIR), help="folder zapisu zapytań i odpowiedzi modelu (domyślnie ./debug)")
    ap.add_argument("--no-debug", action="store_true", help="nie zapisuj odpowiedzi modelu")
    ap.add_argument("--results-dir", default=str(RESULTS_DIR), help="folder zapisu wyników analiz jako JSON (domyślnie ./wyniki)")
    ap.add_argument("--no-results", action="store_true", help="nie zapisuj wyników analiz")
    args = ap.parse_args()

    url = args.llm_url or (LLM_URL if args.backend == "ollama" else "http://127.0.0.1:8080")
    TESSERACT, OCR_LANG = args.tesseract, args.ocr_lang
    keep = args.keep_alive
    keep = int(keep) if re.fullmatch(r"-?\d+", keep) else keep     # Ollama: -1 / 0 jako liczba
    Handler.cfg = {"backend": args.backend, "url": url, "model": args.model,
                   "ctx": max(2048, args.ctx), "max_tokens": max(256, args.max_tokens), "keep_alive": keep}
    Handler.debug_dir = None if args.no_debug else Path(args.debug_dir).resolve()
    Handler.results_dir = None if args.no_results else Path(args.results_dir).resolve()
    srv = Server((args.host, args.port), Handler)
    page = f"http://{args.host}:{args.port}/"
    print(f"Smart Kraken (wersja {VERSION}) działa pod adresem: {page}")
    print(f"Model: {args.model}  ({args.backend} @ {url})")
    print(f"Zapis wyników: {Handler.results_dir}" if Handler.results_dir else "Zapis wyników: wyłączony")
    print(f"Zapis debug: {Handler.debug_dir}" if Handler.debug_dir else "Zapis debug: wyłączony")
    st = llm_status(Handler.cfg)
    if not st["online"]:
        print("UWAGA: serwer modelu nie odpowiada – uruchom Ollamę (lub inny serwer) przed analizą.")
    elif not st["model_ready"]:
        print(f"UWAGA: brak modelu – pobierz go poleceniem:  ollama pull {args.model}")
    else:
        print(f"Okno kontekstu: {st['ctx']} tokenów (model: {st.get('model_max_ctx') or '?'}) · odpowiedź do {st['max_tokens']} "
              f"tokenów · tekst do ok. {st['max_znakow']} znaków · model w pamięci: {keep}")
    o = ocr_status()
    if o["dostepne"]:
        print(f"OCR (skany PDF): Tesseract – {o['program']} · język {OCR_LANG}")
    elif o["program"]:
        print(f"OCR (skany PDF): Tesseract bez języka {OCR_LANG} – doinstaluj dane języka (pol.traineddata)")
    else:
        print("OCR (skany PDF): niedostępne – zainstaluj Tesseract z językiem polskim, "
              "Windows: https://github.com/UB-Mannheim/tesseract/wiki")
    print("Zatrzymanie: Ctrl+C")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(page)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymano.")


if __name__ == "__main__":
    main()
