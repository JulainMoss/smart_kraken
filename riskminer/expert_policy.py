"""Polityka eksperta: automatyczne zatwierdzanie zdarzeń o wysokim prawdopodobieństwie.

Założenie organizatora dla prototypu: ekspert jest entuzjastyczny i zatwierdza wszystkie zdarzenia o wysokim
prawdopodobieństwie. Zatwierdzenie nigdy nie blokuje wiedzy – analizy korzystają ze wszystkich zdarzeń poza odrzuconymi;
polityka sprawia, że zdarzenia pewne mają status „zatwierdzone” (z historią w dzienniku decyzji), a ekspert nadal może
każde z nich odrzucić lub przeklasyfikować. Próg i włączenie: ustawienie `expert.auto_approve` (panel eksperta)."""

from __future__ import annotations

import sqlite3

from .db import setting
from .textutil import now_iso

DEFAULT = {"enabled": True, "min_confidence": 0.8, "reviewer": "ekspert (polityka: zatwierdzanie zdarzeń o wysokim prawdopodobieństwie)"}


def policy(conn: sqlite3.Connection) -> dict:
    return {**DEFAULT, **(setting(conn, "expert.auto_approve", DEFAULT) or {})}


def auto_approve(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> int:
    """Zatwierdza oczekujące zdarzenia z pewnością ≥ progu (opcjonalnie tylko w zakresie `where`). Zwraca liczbę zatwierdzonych."""
    p = policy(conn)
    if not p.get("enabled"):
        return 0
    sql = "SELECT event_id, risk_code, confidence FROM risk_events WHERE status='pending' AND confidence>=?" + (f" AND ({where})" if where else "")
    evs = conn.execute(sql, (float(p["min_confidence"]),) + tuple(params)).fetchall()
    if not evs:
        return 0
    ts = now_iso()
    reviewer = p["reviewer"]
    conn.executemany("UPDATE risk_events SET status='approved', reviewer=?, reviewed_at=? WHERE event_id=?",
                     [(reviewer, ts, e[0]) for e in evs])
    conn.executemany("INSERT INTO expert_log (ts,reviewer,entity_type,entity_id,action,before,after,comment) VALUES (?,?,?,?,?,?,?,?)",
                     [(ts, reviewer, "zdarzenie ryzyka", str(e[0]), "zatwierdzenie automatyczne", '{"status": "pending"}',
                       f'{{"status": "approved", "risk_code": "{e[1]}"}}', f"pewność {e[2]} ≥ próg {p['min_confidence']}") for e in evs])
    conn.commit()
    return len(evs)
