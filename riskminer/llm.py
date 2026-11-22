"""Opcjonalny model językowy (LLM) – wymienny w konfiguracji, bez uzależnienia od dostawcy.

Wszystkie funkcje systemu działają BEZ modelu (reguły + statystyka). Model, jeśli skonfigurowany, służy do:
  * lepszego zrozumienia opisu nowej inwestycji (ekstrakcja parametrów z cytatami),
  * streszczenia raportu prostym językiem (wyraźnie oznaczone jako „wniosek modelu”),
  * propozycji klasyfikacji ryzyka dla zdarzeń, których reguły nie rozpoznały.
Obsługiwane: Ollama (lokalnie, np. Bielik), dowolne API zgodne z OpenAI (llama.cpp, vLLM, Mistral, OpenRouter…)
oraz Claude przez Claude Code w trybie nieinteraktywnym (`claude -p`) – w ramach subskrypcji Claude, bez klucza API.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from typing import Any

from . import config


_tls = threading.local()   # bieżący dostawca w łańcuchu zapasowym (osobno dla każdego wątku serwera)


def cfg() -> dict[str, Any]:
    ov = getattr(_tls, "override", None)
    return ov if ov is not None else config.get()["llm"]


def candidates() -> list[dict[str, Any]]:
    """Główny dostawca + zapasowi z [[llm.fallback]] (np. Claude -> lokalny Bielik -> reguły)."""
    base = {k: v for k, v in config.get()["llm"].items() if k != "fallback"}
    out = [base]
    for f in config.get()["llm"].get("fallback") or []:
        if isinstance(f, dict) and f.get("provider"):
            c = {**base, "effort": "", "claude_bin": "", **f}
            c.setdefault("data_location", "lokalnie")
            out.append(c)
    return [c for c in out if (c.get("provider") or "none").lower() not in ("none", "", "off", "brak")]


def reset_last() -> None:
    _tls.last = None


def last_used() -> dict[str, Any]:
    """Który model faktycznie odpowiedział w ostatnim wywołaniu w tym wątku."""
    return getattr(_tls, "last", None) or {"model": cfg().get("model"), "where": where(), "fallback": False}


def enabled() -> bool:
    return (cfg().get("provider") or "none").lower() not in ("none", "", "off", "brak")


def _http(url: str, payload: dict | None, timeout: float, headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _claude_bin(c: dict[str, Any] | None = None) -> str | None:
    path = (c or cfg()).get("claude_bin")
    if path:
        return path if os.path.isfile(path) else None
    return shutil.which("claude")


def _claude_code(system: str, user: str, schema: dict | None) -> str:
    """Claude przez Claude Code (`claude -p`): bez narzędzi, bez sesji, bez ustawień projektu; wynik zgodny ze schematem JSON."""
    exe = _claude_bin()
    if not exe:
        raise RuntimeError("Nie znaleziono programu 'claude' (Claude Code).")
    c = cfg()
    cmd = [exe, "-p", "Przetwórz dane wejściowe zgodnie z instrukcją systemową.", "--output-format", "json",
           "--system-prompt", system, "--tools", "", "--no-session-persistence", "--strict-mcp-config", "--setting-sources", ""]
    if c.get("model"):
        cmd += ["--model", c["model"]]
    if c.get("effort"):
        cmd += ["--effort", c["effort"]]
    if schema:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    # katalog tymczasowy: żadne pliki projektu (CLAUDE.md itp.) nie trafiają do kontekstu modelu
    r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=c.get("timeout_s", 180), cwd=tempfile.gettempdir())
    try:
        out = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude Code: nieoczekiwana odpowiedź ({(r.stderr or r.stdout)[:200]})")
    if out.get("is_error") or r.returncode != 0:
        raise RuntimeError(f"Claude Code: {out.get('result') or out.get('subtype') or r.stderr[:200]}")
    if schema and isinstance(out.get("structured_output"), dict):
        return json.dumps(out["structured_output"], ensure_ascii=False)
    return out.get("result") or ""


def where() -> str:
    """Krótki opis miejsca przetwarzania do komunikatów w interfejsie."""
    c = cfg()
    if c.get("provider") == "claude-code":
        return "usługa Claude (Anthropic)"
    host = re.sub(r"^https?://", "", c.get("base_url") or "").split("/")[0].split(":")[0]
    return "lokalnie" if host in ("127.0.0.1", "localhost", "0.0.0.0", "") else f"serwer {host}"


def status() -> dict[str, Any]:
    cands = candidates()
    if not cands:
        return _status_one(config.get()["llm"])
    first = _status_one(cands[0])
    if first.get("online") or len(cands) == 1:
        first["fallbacks"] = [f.get("model") for f in cands[1:]]
        return first
    for c in cands[1:]:   # główny niedostępny – pokazujemy pierwszy działający zapasowy
        st = _status_one(c)
        if st.get("online"):
            st["fallback_for"] = cands[0].get("model")
            return st
    first["fallbacks"] = [f.get("model") for f in cands[1:]]
    return first


def _status_one(c: dict[str, Any]) -> dict[str, Any]:
    out = {"provider": c.get("provider"), "model": c.get("model"), "url": c.get("base_url"), "online": False,
           "data_location": c.get("data_location"), "enabled": enabled()}
    if (c.get("provider") or "none").lower() in ("none", "", "off", "brak"):
        out["enabled"] = False
        out["info"] = "Model językowy wyłączony – analiza oparta na regułach eksperckich i statystyce (działa offline)."
        return out
    if c["provider"] == "claude-code":
        exe = _claude_bin(c)
        out["online"] = bool(exe)
        out["model"] = c.get("model") or "domyślny model Claude Code"
        out["url"] = "Claude Code (subskrypcja Claude)"
        if not exe:
            out["error"] = "Nie znaleziono programu 'claude' – zainstaluj Claude Code i zaloguj się (claude, potem /login)."
        return out
    try:
        base = c["base_url"].rstrip("/")
        if c["provider"] == "ollama":
            names = [m.get("name") for m in _http(f"{base}/api/tags", None, 5).get("models", [])]
        else:
            names = [m.get("id") for m in _http(f"{base}/v1/models", None, 8, _auth()).get("data", [])]
        out["online"] = True
        out["models"] = names[:50]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _auth() -> dict:
    key = os.environ.get(cfg().get("api_key_env") or "RISKMINER_LLM_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def chat(system: str, user: str, schema: dict | None = None, max_tokens: int = 1500) -> str:
    """Wywołanie z łańcuchem zapasowym: gdy dostawca nie odpowiada (brak programu, limit, brak sieci), próbujemy kolejnego."""
    errors = []
    cands = candidates()
    for i, c in enumerate(cands):
        _tls.override = c
        try:
            out = _chat_one(system, user, schema, max_tokens)
            _tls.last = {"model": c.get("model"), "provider": c.get("provider"), "where": where(), "fallback": i > 0,
                         "errors": list(errors)}
            return out
        except Exception as e:
            errors.append(f"{c.get('model') or c.get('provider')}: {type(e).__name__}: {str(e)[:120]}")
        finally:
            _tls.override = None
    raise RuntimeError("żaden model nie odpowiedział – " + "; ".join(errors) if errors else "model językowy wyłączony")


def _chat_one(system: str, user: str, schema: dict | None = None, max_tokens: int = 1500) -> str:
    c = cfg()
    if c["provider"] == "claude-code":
        return _claude_code(system, user, schema)
    base = c["base_url"].rstrip("/")
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if c["provider"] == "ollama":
        body = {"model": c["model"], "messages": msgs, "stream": False,
                "options": {"temperature": c.get("temperature", 0), "num_ctx": 8192}}
        if schema:
            body["format"] = schema
        res = _http(f"{base}/api/chat", body, c.get("timeout_s", 180))
        return res.get("message", {}).get("content", "")
    body = {"model": c["model"], "messages": msgs, "temperature": c.get("temperature", 0), "max_tokens": max_tokens}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "wynik", "schema": schema}}
    url = f"{base}/v1/chat/completions" if not base.endswith("/v1") else f"{base}/chat/completions"
    try:
        res = _http(url, body, c.get("timeout_s", 180), _auth())
    except urllib.error.HTTPError as e:
        if schema and e.code in (400, 422):
            body["response_format"] = {"type": "json_object"}
            res = _http(url, body, c.get("timeout_s", 180), _auth())
        else:
            raise
    return res["choices"][0]["message"]["content"]


def chat_json(system: str, user: str, schema: dict | None = None) -> dict:
    content = chat(system, user, schema)
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip())
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        return json.loads(m.group(0)) if m else {}


# etykieta dla modelu -> (sektor, podsektor wod-kan)
SUBSECTOR_LABELS = {
    "kanalizacja sanitarna": ("wod-kan", "kan_san"), "sieć wodociągowa": ("wod-kan", "siec_wod"),
    "oczyszczalnia ścieków": ("wod-kan", "oczyszczalnia"), "stacja uzdatniania wody / ujęcie / zbiornik": ("wod-kan", "suw"),
    "kanalizacja deszczowa / retencja": ("wod-kan", "deszcz"), "inny obiekt wodno-kanalizacyjny": ("wod-kan", "inne_wk"),
    "droga, ulica, most, chodnik, ścieżka rowerowa": ("drogi", ""),
    "budynek (szkoła, przedszkole, hala, termomodernizacja, obiekt publiczny)": ("kubatura", ""),
    "energetyka i OZE (fotowoltaika, pompy ciepła, ciepłownictwo, oświetlenie)": ("energetyka", ""),
    "kolej lub tramwaj": ("kolej", ""),
    "inny rodzaj inwestycji": ("inne", ""),
}
WORKS_LABELS = ["budowa", "rozbudowa", "przebudowa", "modernizacja", "remont/renowacja", ""]
PROVINCE_LABELS = ["dolnośląskie", "kujawsko-pomorskie", "lubelskie", "lubuskie", "łódzkie", "małopolskie", "mazowieckie", "opolskie",
                   "podkarpackie", "podlaskie", "pomorskie", "śląskie", "świętokrzyskie", "warmińsko-mazurskie", "wielkopolskie",
                   "zachodniopomorskie", ""]


def _f(value_schema: dict | None = None) -> dict:
    """Pole = wartość + dosłowny cytat (serwer sprawdza, czy cytat jest w tekście)."""
    return {"type": "object", "properties": {"wartosc": value_schema or {"type": "string"}, "cytat": {"type": "string"}},
            "required": ["wartosc", "cytat"], "additionalProperties": False}


def query_schema(feature_labels: list[str]) -> dict:
    # wszystkie wartości tekstowe – mały model niezawodnie zwraca tekst; liczby parsuje serwer
    return {
        "type": "object",
        "properties": {
            "czy_konkretna_inwestycja": {"type": "boolean"},
            "rodzaj_inwestycji": _f({"type": "string", "enum": list(SUBSECTOR_LABELS)}),
            "rodzaj_robot": _f({"type": "string", "enum": WORKS_LABELS}),
            "zaprojektuj_i_wybuduj": _f({"type": "string", "enum": ["tak", "nie"]}),
            "zamawiajacy": _f(),
            "miejscowosc": _f(),
            "wojewodztwo": _f({"type": "string", "enum": PROVINCE_LABELS}),
            "kwota": {"type": "object", "properties": {"tekst": {"type": "string"}, "vat": {"type": "string", "enum": ["netto", "brutto", "nieokreślone"]},
                                                     "cytat": {"type": "string"}}, "required": ["tekst", "vat", "cytat"], "additionalProperties": False},
            "czas_realizacji": {"type": "object", "properties": {"liczba": {"type": "string"},
                                                               "jednostka": {"type": "string", "enum": ["dni", "tygodnie", "miesiące", "lata", ""]},
                                                               "cytat": {"type": "string"}}, "required": ["liczba", "jednostka", "cytat"], "additionalProperties": False},
            "dlugosc_sieci_km": _f(),
            "liczba_przylaczy": _f(),
            "elementy": {"type": "array", "maxItems": 10, "items": {"type": "string", "enum": feature_labels}},
            "slowa_kluczowe": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
            "streszczenie": {"type": "string"},
        },
        "required": ["czy_konkretna_inwestycja", "rodzaj_inwestycji", "rodzaj_robot", "zaprojektuj_i_wybuduj", "zamawiajacy", "miejscowosc",
                     "wojewodztwo", "kwota", "czas_realizacji", "dlugosc_sieci_km", "liczba_przylaczy", "elementy", "slowa_kluczowe", "streszczenie"],
        "additionalProperties": False,
    }


QUERY_SYSTEM = """Jesteś asystentem urzędnika, który przygotowuje inwestycję publiczną w Polsce.
Z opisu planowanej inwestycji wyciągasz parametry potrzebne do wyszukania podobnych, zrealizowanych inwestycji.
Zwracasz WYŁĄCZNIE obiekt JSON zgodny ze schematem.

Zasady:
- Wartości bierz WYŁĄCZNIE z opisu użytkownika. Nie zgaduj. Gdy czegoś nie ma – "" (pusty tekst) i pusty cytat.
- "cytat" to DOSŁOWNY, krótki fragment opisu (najwyżej 10 słów), z którego pochodzi wartość.
- czy_konkretna_inwestycja: true, gdy opis mówi, CO ma zostać zbudowane lub przebudowane; false, gdy jest zbyt ogólny.
- rodzaj_inwestycji: GŁÓWNY obiekt robót (np. „przebudowa drogi wraz z kanalizacją deszczową” to droga, a nie kanalizacja).
- rodzaj_robot: budowa (nowy obiekt), rozbudowa (powiększenie istniejącego), przebudowa, modernizacja, remont/renowacja.
- zaprojektuj_i_wybuduj: "tak" tylko, gdy wykonawca ma też zaprojektować obiekt (formuła „zaprojektuj i wybuduj”, PFU).
- zamawiajacy: nazwa instytucji (np. „Gmina Lipowo”). miejscowosc: w mianowniku. Bez adresów.
- wojewodztwo: z listy; podaj także, gdy jednoznacznie wynika z nazwy powiatu lub znanego miasta.
- kwota.tekst: kwota CYFRAMI z „mln” lub „tys.” (np. "6 mln zł"; „półtora miliona” -> "1,5 mln zł"); gdy podano zakres
  („od 4 do 5 mln”) – środek zakresu ("4,5 mln zł"); vat: netto / brutto / nieokreślone; cytat – dosłownie z opisu.
- czas_realizacji: liczba (np. "18", "1", "półtora") i jednostka; „rok na realizację” = liczba "1", jednostka "lata".
- dlugosc_sieci_km: łączna długość sieci, rurociągów, drogi lub torów w km (np. "10,7"); gdy podano w metrach – przelicz na km.
- liczba_przylaczy: liczba przyłączy, jeśli podana.
- elementy: tylko elementy z listy, które naprawdę występują w opisie.
- slowa_kluczowe: 3–8 krótkich fraz technicznych do wyszukiwania (np. "kanalizacja grawitacyjna", "przepompownia ścieków",
  "przewiert pod drogą"); bez nazw miejscowości, bez kwot i bez słów ogólnych („inwestycja”, „gmina”).
- streszczenie: jedno zdanie prostym językiem: co, gdzie, jaka skala."""


def understand_query(text: str, feature_labels: list[str]) -> dict:
    return chat_json(QUERY_SYSTEM, "Opis inwestycji:\n\"\"\"\n" + text[:6000] + "\n\"\"\"", query_schema(feature_labels))


SUMMARY_SYSTEM = """Jesteś doradcą urzędnika przygotowującego inwestycję publiczną. Na podstawie danych z raportu
(statystyki podobnych inwestycji, najczęstsze problemy, rekomendacje) napisz krótkie podsumowanie po polsku (5–8 zdań),
prostym językiem, bez żargonu. Używaj WYŁĄCZNIE liczb z danych. Zaznacz niepewność, gdy próba jest mała.
Nie dodawaj informacji spoza danych."""


def summarize_report(facts: dict) -> str:
    return chat(SUMMARY_SYSTEM, json.dumps(facts, ensure_ascii=False)[:12000]).strip()
