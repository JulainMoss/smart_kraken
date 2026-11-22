"""Ewaluacja odczytu opisu inwestycji (formularz parametrów): reguły vs model językowy + reguły.

Uruchomienie (z katalogu repozytorium):
    python tools/eval_odczytu.py            # model z config.toml + reguły, porównanie z samymi regułami
    python tools/eval_odczytu.py --rules    # tylko reguły (bez modelu)

Zbiór wzorcowy: tests/eval/opisy_wzorcowe.json (opis + oczekiwane wartości pól).
Liczby uznajemy za zgodne przy odchyleniu ≤ 10% (czas: ≤ 10% albo ≤ 15 dni)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from riskminer import config, db  # noqa: E402
from riskminer.query import understand  # noqa: E402

NUM = {"budget", "duration_days", "length_km", "connections"}


def ok(field, got, exp) -> bool:
    if field in NUM:
        if got in (None, "") or exp in (None, ""):
            return got in (None, "") and exp in (None, "")
        tol = max(0.10 * abs(exp), 15 if field == "duration_days" else 0)
        return abs(float(got) - float(exp)) <= tol
    if field == "design_build":
        return bool(got) == bool(exp)
    return (got or "") == (exp or "")


def run(use_llm: bool) -> dict:
    config.load()
    db.DB_PATH = config.path("db_path")
    conn = db.connect()
    cases = json.loads((ROOT / "tests/eval/opisy_wzorcowe.json").read_text(encoding="utf-8"))
    per_field: dict[str, list[int]] = {}
    errors = []
    t0 = time.time()
    for c in cases:
        q = understand(conn, c["tekst"], use_llm=use_llm)
        P = {k: v.get("value") for k, v in q["params"].items()}
        for f, exp in c["oczekiwane"].items():
            good = ok(f, P.get(f), exp)
            per_field.setdefault(f, []).append(int(good))
            if not good:
                errors.append(f"{c['id']}: {f} = {P.get(f)!r} (oczekiwano {exp!r}; źródło: {q['params'].get(f, {}).get('source')})")
    total = sum(sum(v) for v in per_field.values())
    n = sum(len(v) for v in per_field.values())
    return {"accuracy": total / n, "n": n, "per_field": {f: f"{sum(v)}/{len(v)}" for f, v in per_field.items()},
            "errors": errors, "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    modes = [False] if "--rules" in sys.argv else [False, True]
    for m in modes:
        r = run(m)
        print(f"\n=== {'model + reguły' if m else 'same reguły'}: trafność {r['accuracy']:.0%} ({r['n']} pól, {r['seconds']} s)")
        print("   pola:", r["per_field"])
        for e in r["errors"]:
            print("   ✗", e)
