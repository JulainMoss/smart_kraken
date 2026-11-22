"""Konfiguracja: plik config.toml (kopia config.example.toml) + zmienne środowiskowe RISKMINER_*.
Zmiana modelu / dostawcy nie wymaga zmian w kodzie."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8000, "open_browser": True},
    "data": {
        "db_path": "data/riskminer.sqlite",
        "bzp_mirror_jsonl": "../risk_miner_bzp_kopia/bzp_kopia/notices.jsonl",
        "data_lake": "../risk_miner_bzp_kopia/data_lake",
        "uploads": "data/uploads",
    },
    "sources": {
        # API BZP: kopia hackathonowa bez limitu (ten sam interfejs) albo produkcja (30 zapytań/min)
        "bzp_api": "https://bzp.cytr.us",
        "bzp_api_fallback": "https://ezamowienia.gov.pl",
        "ted_api": "https://api.ted.europa.eu/v3/notices/search",
        "saos_api": "https://www.saos.org.pl/api",
        "user_agent": "RiskMiner/1.0 (HackNation 2026; prototyp MFiPR)",
        "request_delay_s": 0.5,
        "bip_max_pages": 40,
    },
    "llm": {
        # provider: none (tylko reguły – działa zawsze), ollama, openai (dowolne API zgodne z OpenAI:
        # OpenAI, Mistral (UE), OpenRouter, Groq, vLLM, llama.cpp, LM Studio)
        "provider": "none",
        "base_url": "http://127.0.0.1:11434",
        "model": "SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M",
        "api_key_env": "RISKMINER_LLM_API_KEY",
        "temperature": 0.0,
        "timeout_s": 180,
        "data_location": "lokalnie (bez modelu językowego – dane nie opuszczają komputera)",
        "effort": "",          # tylko claude-code: low / medium / high
        "claude_bin": "",      # tylko claude-code: ścieżka do programu claude (domyślnie z PATH)
    },
    "expert": {"default_reviewer": "ekspert"},
}

_cfg: dict[str, Any] | None = None


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: Path | None = None) -> dict[str, Any]:
    global _cfg
    cfg = DEFAULTS
    p = path or Path(os.environ.get("RISKMINER_CONFIG", ROOT / "config.toml"))
    if p.exists() and tomllib:
        with p.open("rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
    # zmienne środowiskowe mają pierwszeństwo, np. RISKMINER_LLM_PROVIDER=openai
    for sect, vals in cfg.items():
        if not isinstance(vals, dict):
            continue
        for k in list(vals):
            env = os.environ.get(f"RISKMINER_{sect}_{k}".upper())
            if env is not None:
                cur = vals[k]
                vals[k] = (env.lower() in ("1", "true", "tak", "yes")) if isinstance(cur, bool) else (type(cur)(env) if isinstance(cur, (int, float)) else env)
    _cfg = cfg
    return cfg


def get() -> dict[str, Any]:
    return _cfg if _cfg is not None else load()


def path(key: str) -> Path:
    p = Path(get()["data"][key])
    return p if p.is_absolute() else (ROOT / p).resolve()
