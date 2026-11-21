"""Narzędzia tekstowe: HTML -> tekst, normalizacja polskich słów, kwoty, daty, okresy, NIP/REGON, statystyka."""

from __future__ import annotations

import datetime as dt
import html
import math
import re
import unicodedata
import zlib

WS_RE = re.compile(r"[ \t\r\f\v\xa0]+")
TOKEN_RE = re.compile(r"[0-9a-ząćęłńóśźż]+", re.IGNORECASE)

STOPWORDS = set("""
a aby albo ale ani az bez bo by byc być byl był byla była bylo było beda będą bedzie będzie co czy dla do gdy gdzie i ich
ile im in ja jak jako je jego jej jest jesli jeśli jez jeż juz już ktora która ktore które ktory który ma maja mają
mi na nad nam nas nie niz niż o od oraz po pod przez przy sa są sie się sobie ta tak tam te tego tej ten to tu tych ty
tym w we wraz z za ze że zl zł r nr ul pkt ust art tj m.in szt mb dz poz itp itd oraz lub wg dot ok ul.
roku rok zadania zadanie zakresie zakres realizacja realizacji przedmiot przedmiotem zamowienia zamówienia zamowienie zamówienie
gmina gminie gminy miejscowosci miejscowości
""".split())


def html_to_text(raw: str | None) -> str:
    """Zamienia HTML ogłoszenia BZP na czysty tekst z zachowaniem podziału na linie."""
    if not raw:
        return ""
    h = re.sub(r"<style.*?</style>", " ", raw, flags=re.S | re.I)
    h = re.sub(r"<script.*?</script>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<(br|/p|/div|/h\d|/li|/tr|/table|h\d)[^>]*>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html.unescape(h)
    h = WS_RE.sub(" ", h)
    h = re.sub(r" *\n[ \n]*", "\n", h)
    return h.strip()


def fold(s: str | None) -> str:
    """Małe litery bez polskich znaków diakrytycznych."""
    s = (s or "").lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def stem(tok: str) -> str:
    """Bardzo prosty stemmer dla polskiego: obcięcie do 6 znaków (po usunięciu diakrytyków).
    Działa dobrze dla dopasowania odmian: kanalizacji/kanalizacyjnej -> kanali, wodociągowej -> wodoci."""
    t = fold(tok)
    if t.isdigit():
        return t
    return t[:6] if len(t) > 6 else t


def tokens(text: str | None, keep_stop: bool = False) -> list[str]:
    out = []
    for t in TOKEN_RE.findall((text or "").lower()):
        if len(t) < 2:
            continue
        if not keep_stop and (t in STOPWORDS or fold(t) in STOPWORDS):
            continue
        out.append(t)
    return out


def stems(text: str | None) -> list[str]:
    return [stem(t) for t in tokens(text)]


def stem_string(text: str | None) -> str:
    return " ".join(stems(text))


# ---------------------------------------------------------------- kwoty i liczby
_MULT = [(r"mld|miliard", 1e9), (r"mln|milion", 1e6), (r"tys|tysi", 1e3)]


def parse_amount(s) -> float | None:
    """'4,2 mln zł' -> 4200000.0; '850 000,50' -> 850000.5; '669273,44 PLN' -> 669273.44"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).lower().replace("\xa0", " ")
    m = re.search(r"\d[\d\s.,]*", s)
    if not m:
        return None
    num = re.sub(r"\s+", "", m.group(0)).rstrip(".,")
    has_mult = re.search(r"(mld|mln|tys|miliard|milion|tysi)", s[m.end():m.end() + 14] or "")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        if re.search(r",\d{1,2}$", num) or has_mult:
            num = num.replace(",", ".")
        else:
            num = num.replace(",", "")
    elif num.count(".") > 1 or (re.search(r"\.\d{3}$", num) and not has_mult):
        num = num.replace(".", "")
    try:
        val = float(num)
    except ValueError:
        return None
    rest = s[m.end():m.end() + 14]
    for pat, mul in _MULT:
        if re.match(r"\s*(" + pat + ")", rest):
            val *= mul
            break
    return round(val, 2)


def parse_date(s) -> dt.date | None:
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(s or ""))
    if not m:
        m2 = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", str(s or ""))
        if not m2:
            return None
        d, mo, y = m2.groups()
        try:
            return dt.date(int(y), int(mo), int(d))
        except ValueError:
            return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


_UNIT_DAYS = [
    (r"dni|dzie[nń]|dniach|dnia", 1.0),
    (r"tydz|tygod", 7.0),
    (r"miesi|m-c|mies", 30.4375),
    (r"lat|rok|roku", 365.25),
]
_WORD_NUM = {"jeden": 1, "jednego": 1, "dwa": 2, "dwóch": 2, "dwoch": 2, "dwie": 2, "trzy": 3, "trzech": 3,
             "cztery": 4, "czterech": 4, "pięć": 5, "piec": 5, "pięciu": 5, "sześć": 6, "szesc": 6, "sześciu": 6,
             "siedem": 7, "osiem": 8, "dziewięć": 9, "dziesięć": 10, "dwanaście": 12, "osiemnaście": 18,
             "dwadzieścia": 20, "półtora": 1.5, "pol": 0.5}


def parse_period_days(s: str | None, start: dt.date | None = None) -> int | None:
    """Okres realizacji -> liczba dni. Obsługuje '5 miesiące', '470 dni', 'Od 2021-02-09 do 2021-07-30',
    'do 2021-07-30' (wymaga daty startu), '2 lata'."""
    if not s:
        return None
    t = s.lower()
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", t)
    if len(dates) >= 2:
        a, b = parse_date(dates[0]), parse_date(dates[1])
        if a and b and b >= a:
            return (b - a).days
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(dni|dzie[nń]|tydz\w*|tygod\w*|miesi\w*|m-c\w*|lat\w*|rok\w*)", t)
    if m:
        n = float(m.group(1).replace(",", "."))
        unit = m.group(2)
        for pat, mul in _UNIT_DAYS:
            if re.match(pat, unit):
                return int(round(n * mul))
    if len(dates) == 1 and start:
        b = parse_date(dates[0])
        if b and b >= start:
            return (b - start).days
    return None


# ---------------------------------------------------------------- identyfikatory
def digits(s) -> str:
    return re.sub(r"\D+", "", str(s or ""))


def nip_valid(nip: str | None) -> bool:
    d = digits(nip)
    if len(d) != 10:
        return False
    w = [6, 5, 7, 2, 3, 4, 5, 6, 7]
    c = sum(int(d[i]) * w[i] for i in range(9)) % 11
    return c != 10 and c == int(d[9])


def regon_valid(regon: str | None) -> bool:
    d = digits(regon)
    if len(d) == 9:
        w = [8, 9, 2, 3, 4, 5, 6, 7]
    elif len(d) == 14:
        w = [2, 4, 8, 5, 0, 9, 7, 3, 6, 1, 2, 4, 8]
    else:
        return False
    c = sum(int(d[i]) * w[i] for i in range(len(w))) % 11
    return (0 if c == 10 else c) == int(d[len(w)])


def norm_bzp_number(s: str | None) -> str | None:
    """Normalizuje numer ogłoszenia BZP: '2022/BZP 58537/01', '2022/BZP00058537' -> '2022/BZP 00058537/01'."""
    if not s:
        return None
    m = re.search(r"(20\d\d)\s*/\s*BZP\s*0*(\d{1,8})(?:\s*/\s*(\d{1,2}))?", str(s), re.I)
    if not m:
        return None
    ver = m.group(3) or "01"
    return f"{m.group(1)}/BZP {int(m.group(2)):08d}/{int(ver):02d}"


def bzp_base(s: str | None) -> str | None:
    n = norm_bzp_number(s)
    return n[:-3] if n else None


def norm_ted_number(s: str | None) -> str | None:
    m = re.search(r"\b(\d{1,6})\s*-\s*(20\d\d)\b", str(s or ""))
    return f"{int(m.group(1))}-{m.group(2)}" if m else None


# ---------------------------------------------------------------- kompresja tekstu
def zpack(s: str | None) -> bytes | None:
    if not s:
        return None
    return zlib.compress(s.encode("utf-8"), 6)


def zunpack(b: bytes | None) -> str:
    if not b:
        return ""
    return zlib.decompress(b).decode("utf-8")


# ---------------------------------------------------------------- statystyka
def wilson(k: int, n: int, z: float = 1.645) -> tuple[float, float]:
    """Przedział Wilsona (domyślnie 90%) dla proporcji k/n."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def quantile(vals: list[float], q: float) -> float | None:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = (len(v) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def weighted_share(flags: list[tuple[bool, float]]) -> float | None:
    tot = sum(w for _, w in flags)
    if tot <= 0:
        return None
    return sum(w for f, w in flags if f) / tot


def fmt_pln(v: float | None) -> str:
    if v is None:
        return "b.d."
    if abs(v) >= 1e6:
        return f"{v / 1e6:,.2f} mln zł".replace(",", " ").replace(".", ",")
    if abs(v) >= 1e3:
        return f"{v / 1e3:,.0f} tys. zł".replace(",", " ")
    return f"{v:,.0f} zł".replace(",", " ")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
