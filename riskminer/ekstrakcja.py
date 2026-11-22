"""Ekstrakcja parametrów inwestycji z opisu modelem językowym – instrukcja (prompt), schemat odpowiedzi
i weryfikacja odpowiedzi z tekstem. Przeniesione 1:1 z narzędzia ekstraktora LLM (dawniej tools/ekstraktor_llm,
port 8001), żeby Risk Miner pytał model dokładnie tak samo:

  * ta sama instrukcja systemowa (SYSTEM_PROMPT) i ten sam schemat JSON (SCHEMA, dekodowanie z ograniczeniem),
  * te same opcje modelu (stałe okno kontekstu, limit odpowiedzi, keep_alive, temperatura 0),
  * ta sama weryfikacja: cytaty w tekście, odrzucanie kwot jako zamawiającego i dat spoza tekstu,
    reguły zapasowe, adresy usuwane, czas od daty podpisania umowy, zakres rzeczowy w liczbach (km → m)
    z jednostką z cytatu i pozycjami pominiętymi przez model, dopisanymi regułą.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata

TEMPERATURE = 0.0
CTX = 8192                     # STAŁE okno kontekstu (zmiana między zapytaniami = przeładowanie modelu w Ollamie)
MAX_TOKENS = 1536              # limit długości odpowiedzi (zabezpieczenie przed zapętleniem modelu)
KEEP_ALIVE = "30m"             # jak długo Ollama trzyma model w pamięci po analizie
MAX_TEXT_CHARS = 60000         # bezwzględny limit tekstu
PROMPT_RESERVE_TOKENS = 1400   # instrukcja + schemat (ok. 1000 tokenów) z zapasem
CHARS_PER_TOKEN = 2.3          # ostrożny przelicznik dla polskiego tekstu


def text_budget(ctx: int = CTX, max_tokens: int = MAX_TOKENS) -> int:
    """Ile znaków tekstu zmieści się w oknie kontekstu obok instrukcji i odpowiedzi."""
    tokens = ctx - max_tokens - PROMPT_RESERVE_TOKENS
    return max(2000, min(MAX_TEXT_CHARS, int(tokens * CHARS_PER_TOKEN)))


def messages(text: str, today: dt.date) -> tuple[str, str]:
    """(instrukcja systemowa, wiadomość użytkownika) – dokładnie to, co dostaje model."""
    system = SYSTEM_PROMPT.format(today=today.isoformat(), woj=", ".join(WOJEWODZTWA))
    user = ("Tekst do analizy:\n\"\"\"\n" + text + "\n\"\"\"\n\n"
            "Zwróć JSON z polami: zamawiajacy, rodzaj_zamowienia, miejscowosc, wojewodztwo, "
            "planowana_kwota, planowany_czas, zakres_rzeczowy, slowa_kluczowe.")
    return system, user


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
    for m in re.finditer(_NUM_TXT + r"\s*" + _UNIT_RE + r"((?:\s+(?!(?:oraz|i|a|lub|wraz|albo|ze|z\s+\d)\b)[\w./-]+){0,8})", text, re.I):
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
