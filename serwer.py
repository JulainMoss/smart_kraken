#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart Kraken – analiza opisu inwestycji lokalnym modelem językowym (LLM).

Strona (index.html) przyjmuje tekst w języku naturalnym, a serwer przekazuje go
do lokalnego, otwartego modelu LLM, który wyciąga:
  zamawiającego, rodzaj zamówienia, miejscowość, województwo,
  planowaną kwotę, planowany czas realizacji i słowa kluczowe inwestycji.

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

Wymagania: Python 3.8+, wyłącznie biblioteka standardowa.
"""

import argparse
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

# ---- ustawienia (można zmienić tutaj albo opcjami wiersza poleceń) -------------------
BACKEND = "ollama"                                         # --backend: ollama | openai
LLM_URL = "http://127.0.0.1:11434"                         # --llm-url
MODEL = "SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M"       # --model
TEMPERATURE = 0.0
LLM_TIMEOUT_S = 600            # długie teksty na CPU potrafią liczyć się kilka minut
MAX_TEXT_CHARS = 60000         # dłuższy tekst jest przycinany (z ostrzeżeniem)
MAX_CTX = 32768                # górny limit okna kontekstu przekazywany do Ollamy
DEBUG_DIR = ROOT / "debug"     # zapis każdego zapytania i odpowiedzi modelu (--debug-dir, --no-debug)

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
                           "termin_do": {"type": "string"},
                           "cytat": {"type": "string"}},
            "required": ["liczba", "jednostka", "termin_do", "cytat"],
        },
        "slowa_kluczowe": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"fraza": {"type": "string"},
                                     "kategoria": {"type": "string", "enum": KATEGORIE}},
                      "required": ["fraza", "kategoria"]},
        },
    },
    "required": ["zamawiajacy", "rodzaj_zamowienia", "miejscowosc", "wojewodztwo",
                 "planowana_kwota", "planowany_czas", "slowa_kluczowe"],
}

SYSTEM_PROMPT = """Jesteś asystentem analizującym opisy planowanych inwestycji i zamówień publicznych w Polsce.
Z tekstu użytkownika wyciągasz dane i zwracasz WYŁĄCZNIE obiekt JSON zgodny ze schematem.

Zasady:
- Korzystaj tylko z informacji zawartych w tekście. Niczego nie zmyślaj.
- Gdy informacji brak – wpisz pusty tekst "" (dla województwa: "wywnioskowane": false).
- "cytat" to DOSŁOWNY fragment tekstu (najwyżej ok. 25 słów), z którego pochodzi wartość; "" gdy brak.
- zamawiajacy: pełna nazwa instytucji (np. "Gmina Rydzyna", "Przedsiębiorstwo Wodociągów i Kanalizacji Sp. z o.o.").
- rodzaj_zamowienia: "roboty budowlane" (budowa, przebudowa, remont, modernizacja, także „zaprojektuj i wybuduj”),
  "dostawy" (zakup sprzętu, materiałów, urządzeń), "usługi" (projekty, nadzór, utrzymanie, serwis, odbiór).
- miejscowosc: główna miejscowość inwestycji (w mianowniku, np. "Rydzyna", nie "w Rydzynie").
- wojewodztwo: jedna z nazw z listy, małymi literami. Jeśli nie pada w tekście, ale wynika jednoznacznie
  z miejscowości lub zamawiającego – podaj je i ustaw "wywnioskowane": true; jeśli nie masz pewności – "".
- planowana_kwota.kwota: sama liczba w złotych (lub w podanej walucie), bez spacji i jednostek, z kropką
  dziesiętną, np. "4200000" dla „4,2 mln zł”, "850000.50" dla „850 000,50 zł”. waluta np. "PLN".
  vat: "netto", "brutto" albo "nieokreślone".
- planowany_czas: czas realizacji jako liczba + jednostka (np. "18" i "miesiące"); jeśli podano termin końcowy,
  wpisz go w termin_do w formacie RRRR-MM-DD (np. „do końca 2027 r.” → "2027-12-31"). Dzisiejsza data: {today}.
- slowa_kluczowe: 6–15 krótkich fraz (1–5 słów) opisujących najważniejsze cechy inwestycji: zakres robót,
  obiekty, parametry (długości, średnice, liczby), technologie, formułę realizacji, finansowanie,
  terminy i ryzyka. Każdej przypisz kategorię z listy. Bez powtórzeń.

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


def parse_date(s):
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def postprocess(raw, text, today):
    """Porządkuje odpowiedź modelu: normalizacja wartości, sprawdzenie cytatów, przeliczenia."""
    def g(obj, key, default=""):
        v = (obj or {}).get(key, default)
        return v if v is not None else default

    out = {}
    for key in ("zamawiajacy", "miejscowosc"):
        o = raw.get(key) or {}
        out[key] = {"wartosc": str(g(o, "wartosc")).strip(), "cytat": str(g(o, "cytat")).strip()}

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
    liczba = parse_amount(g(o, "liczba"))
    jedn = g(o, "jednostka")
    jedn = jedn if jedn in JEDNOSTKI else ""
    termin = parse_date(g(o, "termin_do"))
    dni = None
    if liczba is not None and jedn:
        dni = round(liczba * {"dni": 1, "tygodnie": 7, "miesiące": 30.4375, "lata": 365.25}[jedn])
    elif termin:
        dni = (termin - today).days
    out["planowany_czas"] = {"liczba": liczba, "jednostka": jedn if liczba is not None else "",
                             "termin_do": termin.isoformat() if termin else "", "dni": dni,
                             "cytat": str(g(o, "cytat")).strip()}

    kws, seen = [], set()
    for k in raw.get("slowa_kluczowe") or []:
        if isinstance(k, str):
            k = {"fraza": k, "kategoria": "inne"}
        fr = str((k or {}).get("fraza", "")).strip(" .,;")
        if not fr or fold(fr) in seen:
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
    return out


# ---- komunikacja z modelem --------------------------------------------------------------
def http_json(url, payload=None, timeout=LLM_TIMEOUT_S):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


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


def call_llm(cfg, text, today, trace=None):
    """Wysyła tekst do modelu. `trace` (słownik) dostaje zapytanie i pełną odpowiedź – do zapisu debug."""
    trace = trace if trace is not None else {}
    system = SYSTEM_PROMPT.format(today=today.isoformat(), woj=", ".join(WOJEWODZTWA))
    user = ("Tekst do analizy:\n\"\"\"\n" + text + "\n\"\"\"\n\n"
            "Zwróć JSON z polami: zamawiajacy, rodzaj_zamowienia, miejscowosc, wojewodztwo, "
            "planowana_kwota, planowany_czas, slowa_kluczowe.")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    base = cfg["url"].rstrip("/")
    if cfg["backend"] == "ollama":
        ctx = min(MAX_CTX, max(4096, int(len(text) / 2.5) + 3072))
        body = {"model": cfg["model"], "messages": messages, "stream": False, "format": SCHEMA,
                "options": {"temperature": TEMPERATURE, "num_ctx": ctx}}
        trace["request"] = {"url": f"{base}/api/chat", "body": body}
        res = http_json(f"{base}/api/chat", body)
        trace["response"] = res
        content = res.get("message", {}).get("content", "")
        stats = {"tokens_in": res.get("prompt_eval_count"), "tokens_out": res.get("eval_count")}
    else:
        body = {"model": cfg["model"], "messages": messages, "temperature": TEMPERATURE,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "inwestycja", "schema": SCHEMA, "strict": True}}}
        trace["request"] = {"url": f"{base}/v1/chat/completions", "body": body}
        try:
            res = http_json(f"{base}/v1/chat/completions", body)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 422):
                raise
            trace["json_schema_rejected"] = f"HTTP {e.code}: " + e.read().decode("utf-8", "replace")[:500]
            body = dict(body, response_format={"type": "json_object"})   # starsze serwery bez json_schema
            trace["request"] = {"url": f"{base}/v1/chat/completions", "body": body}
            res = http_json(f"{base}/v1/chat/completions", body)
        trace["response"] = res
        content = res["choices"][0]["message"]["content"]
        u = res.get("usage") or {}
        stats = {"tokens_in": u.get("prompt_tokens"), "tokens_out": u.get("completion_tokens")}
    trace["content"] = content
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


def rel_path(path):
    if not path:
        return None
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# ---- serwer HTTP --------------------------------------------------------------------------
class Handler(http.server.SimpleHTTPRequestHandler):
    cfg = {}
    debug_dir = DEBUG_DIR

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/api/status":
            return self._json(200, llm_status(self.cfg))
        if path == "/":
            self.path = "/index.html"
            path = "/index.html"
        if ".." in path or not (path in STATIC_FILES or path.startswith(STATIC_DIRS)):
            return self.send_error(404, "Nie znaleziono")
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
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
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
            warnings.append(f"Tekst przycięto do {MAX_TEXT_CHARS} znaków.")
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

        try:
            raw, content, stats = call_llm(self.cfg, text, today, trace)
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
        result = postprocess(raw if isinstance(raw, dict) else {}, text, today)
        elapsed = round(time.time() - t0, 1)
        record.update(status="ok", czas_s=elapsed, statystyki=stats,
                      odpowiedz_sparsowana=raw, wynik_po_obrobce=result)
        path = save_debug(self.debug_dir, record)
        return self._json(200, {
            "wynik": result, "surowa_odpowiedz": raw, "surowa_tresc": content,
            "model": self.cfg["model"], "backend": self.cfg["backend"],
            "czas_s": elapsed, "statystyki": stats, "ostrzezenia": warnings,
            "dlugosc_tekstu": len(text),
            "debug_plik": rel_path(path),
        })

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description="Smart Kraken – analiza opisu inwestycji lokalnym LLM")
    ap.add_argument("--backend", choices=["ollama", "openai"], default=BACKEND,
                    help="rodzaj serwera modelu: ollama (domyślnie) albo openai (llama.cpp, LM Studio, vLLM…)")
    ap.add_argument("--llm-url", default=None, help=f"adres serwera modelu (domyślnie {LLM_URL} dla ollama, http://127.0.0.1:8080 dla openai)")
    ap.add_argument("--model", default=MODEL, help=f"nazwa modelu (domyślnie {MODEL})")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="nie otwieraj przeglądarki")
    ap.add_argument("--debug-dir", default=str(DEBUG_DIR), help="folder zapisu zapytań i odpowiedzi modelu (domyślnie ./debug)")
    ap.add_argument("--no-debug", action="store_true", help="nie zapisuj odpowiedzi modelu")
    args = ap.parse_args()

    url = args.llm_url or (LLM_URL if args.backend == "ollama" else "http://127.0.0.1:8080")
    Handler.cfg = {"backend": args.backend, "url": url, "model": args.model}
    Handler.debug_dir = None if args.no_debug else Path(args.debug_dir).resolve()
    srv = Server((args.host, args.port), Handler)
    page = f"http://{args.host}:{args.port}/"
    print(f"Smart Kraken działa pod adresem: {page}")
    print(f"Model: {args.model}  ({args.backend} @ {url})")
    print(f"Zapis debug: {Handler.debug_dir}" if Handler.debug_dir else "Zapis debug: wyłączony")
    st = llm_status(Handler.cfg)
    if not st["online"]:
        print("UWAGA: serwer modelu nie odpowiada – uruchom Ollamę (lub inny serwer) przed analizą.")
    elif not st["model_ready"]:
        print(f"UWAGA: brak modelu – pobierz go poleceniem:  ollama pull {args.model}")
    print("Zatrzymanie: Ctrl+C")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(page)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymano.")


if __name__ == "__main__":
    main()
