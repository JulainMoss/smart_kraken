"""Opcjonalny model językowy (LLM) – wymienny w konfiguracji, bez uzależnienia od dostawcy.

Wszystkie funkcje systemu działają BEZ modelu (reguły + statystyka). Model, jeśli skonfigurowany, służy do:
  * lepszego zrozumienia opisu nowej inwestycji (ekstrakcja parametrów z cytatami),
  * streszczenia raportu prostym językiem (wyraźnie oznaczone jako „wniosek modelu”),
  * propozycji klasyfikacji ryzyka dla zdarzeń, których reguły nie rozpoznały.
Obsługiwane: Ollama (lokalnie, np. Bielik) oraz dowolne API zgodne z OpenAI (OpenAI, Mistral, OpenRouter, Groq, vLLM, llama.cpp).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from . import config


def cfg() -> dict[str, Any]:
    return config.get()["llm"]


def enabled() -> bool:
    return (cfg().get("provider") or "none").lower() not in ("none", "", "off", "brak")


def _http(url: str, payload: dict | None, timeout: float, headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def status() -> dict[str, Any]:
    c = cfg()
    out = {"provider": c.get("provider"), "model": c.get("model"), "url": c.get("base_url"), "online": False,
           "data_location": c.get("data_location"), "enabled": enabled()}
    if not enabled():
        out["info"] = "Model językowy wyłączony – analiza oparta na regułach eksperckich i statystyce (działa offline)."
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
    c = cfg()
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


QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "zamawiajacy": {"type": "string"}, "miejscowosc": {"type": "string"}, "wojewodztwo": {"type": "string"},
        "podsektor": {"type": "string", "enum": ["siec_wod", "kan_san", "oczyszczalnia", "suw", "deszcz", "inne_wk", "inny_sektor"]},
        "rodzaj_robot": {"type": "string", "enum": ["budowa", "rozbudowa", "przebudowa", "modernizacja", "remont/renowacja", ""]},
        "zaprojektuj_i_wybuduj": {"type": "boolean"},
        "kwota_pln": {"type": "number"}, "kwota_vat": {"type": "string", "enum": ["netto", "brutto", "nieokreślone"]},
        "czas_miesiace": {"type": "number"}, "dlugosc_km": {"type": "number"}, "liczba_przylaczy": {"type": "number"},
        "elementy": {"type": "array", "items": {"type": "string"}},
        "ryzyka_wspomniane": {"type": "array", "items": {"type": "string"}},
        "streszczenie": {"type": "string"},
    },
    "required": ["podsektor", "streszczenie"],
}

QUERY_SYSTEM = """Jesteś ekspertem inwestycji publicznych w Polsce (sektor wodno-kanalizacyjny i inne).
Z opisu planowanej inwestycji wyciągasz parametry do wyszukania podobnych, zrealizowanych inwestycji.
Zwracasz WYŁĄCZNIE JSON zgodny ze schematem. Nie zmyślaj – brak informacji = pomiń pole lub 0.
podsektor: siec_wod (sieć wodociągowa), kan_san (kanalizacja sanitarna), oczyszczalnia (oczyszczalnia ścieków),
suw (stacja uzdatniania, ujęcia, zbiorniki wody), deszcz (kanalizacja deszczowa i retencja), inne_wk, inny_sektor.
kwota_pln: liczba w złotych (np. 4,2 mln -> 4200000). czas_miesiace: planowany czas realizacji.
elementy: krótkie frazy techniczne (np. "przepompownia", "przewiert pod drogą wojewódzką").
streszczenie: 1–2 zdania prostym językiem."""


def understand_query(text: str) -> dict:
    return chat_json(QUERY_SYSTEM, "Opis inwestycji:\n\"\"\"\n" + text[:12000] + "\n\"\"\"", QUERY_SCHEMA)


SUMMARY_SYSTEM = """Jesteś doradcą urzędnika przygotowującego inwestycję publiczną. Na podstawie danych z raportu
(statystyki podobnych inwestycji, najczęstsze problemy, rekomendacje) napisz krótkie podsumowanie po polsku (5–8 zdań),
prostym językiem, bez żargonu. Używaj WYŁĄCZNIE liczb z danych. Zaznacz niepewność, gdy próba jest mała.
Nie dodawaj informacji spoza danych."""


def summarize_report(facts: dict) -> str:
    return chat(SUMMARY_SYSTEM, json.dumps(facts, ensure_ascii=False)[:12000]).strip()
