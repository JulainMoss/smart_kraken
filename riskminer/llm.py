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
        # te same opcje co w ekstraktorze: stałe okno kontekstu (bez przeładowań modelu), limit odpowiedzi, keep_alive
        body = {"model": c["model"], "messages": msgs, "stream": False, "keep_alive": c.get("keep_alive") or "30m",
                "options": {"temperature": c.get("temperature", 0), "num_ctx": int(c.get("ctx") or 8192), "num_predict": max_tokens}}
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


def chat_json(system: str, user: str, schema: dict | None = None, max_tokens: int = 1500) -> dict:
    content = chat(system, user, schema, max_tokens)
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip())
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        return json.loads(m.group(0)) if m else {}


def extract_params(text: str, today=None) -> dict:
    """Parametry inwestycji z opisu – ta sama instrukcja, schemat i opcje co w ekstraktorze LLM (riskminer.ekstrakcja).
    Zwraca surową odpowiedź modelu; weryfikację z tekstem robi ekstrakcja.postprocess()."""
    import datetime as dt
    from . import ekstrakcja as ek
    today = today or dt.date.today()
    c = cfg()
    budget = ek.text_budget(int(c.get("ctx") or ek.CTX), int(c.get("max_tokens") or ek.MAX_TOKENS))
    system, user = ek.messages(text[:budget], today)
    return chat_json(system, user, ek.SCHEMA, max_tokens=int(c.get("max_tokens") or ek.MAX_TOKENS))


SUMMARY_SYSTEM = """Jesteś doradcą urzędnika przygotowującego inwestycję publiczną. Na podstawie danych z raportu
(statystyki podobnych inwestycji, najczęstsze problemy, rekomendacje) napisz krótkie podsumowanie po polsku (5–8 zdań),
prostym językiem, bez żargonu. Używaj WYŁĄCZNIE liczb z danych. Zaznacz niepewność, gdy próba jest mała.
Nie dodawaj informacji spoza danych."""


def summarize_report(facts: dict) -> str:
    return chat(SUMMARY_SYSTEM, json.dumps(facts, ensure_ascii=False)[:12000]).strip()
