"""Raport z testu łączenia: zestawienie poprawnych, niepewnych i błędnych połączeń między bazami (wymóg MFiPR)."""

from __future__ import annotations

import sqlite3

from .db import rows, unj
from .textutil import now_iso


def link_report(conn: sqlite3.Connection) -> str:
    projects = rows(conn, "SELECT * FROM projects ORDER BY project_id")
    L = [f"# Raport z testu łączenia – Risk Miner\n\nWygenerowano: {now_iso()} · projektów: {len(projects)}\n",
         "Kategorie: **pewne** – dopasowanie po identyfikatorze (numer ogłoszenia BZP) zgodne z NIP i nazwą; **niepewne** – dopasowanie "
         "przybliżone (NIP + nazwa/wartość/czas) lub rozbieżność danych, wymaga decyzji eksperta; **błędne/odrzucone** – odrzucone przez eksperta; "
         "**brak** – nie znaleziono postępowania.\n",
         "| Projekt | Identyfikator publiczny | Powiązanie | Podstawa | Pewność | Kategoria | Uzasadnienie |", "|---|---|---|---|---|---|---|"]
    stats = {"pewne": 0, "niepewne": 0, "błędne/odrzucone": 0, "brak": 0, "zatwierdzone przez eksperta": 0}
    for p in projects:
        links = rows(conn, "SELECT * FROM project_links WHERE project_id=? AND target_type IN ('procedure','missing') ORDER BY confidence DESC", (p["project_id"],))
        if not links:
            stats["brak"] += 1
            L.append(f"| {p['project_id']} | {p.get('public_id')} | — | — | — | brak | projekt nie był jeszcze wzbogacany |")
            continue
        for l in links:
            if l["target_type"] == "missing":
                cat = "brak"
            elif l["status"] == "rejected":
                cat = "błędne/odrzucone"
            elif l["status"] == "approved":
                cat = "zatwierdzone przez eksperta"
            elif l["status"] == "auto":
                cat = "pewne"
            else:
                cat = "niepewne"
            stats[cat] += 1
            ev = "; ".join(unj(l.get("evidence"), []))[:220]
            L.append(f"| {p['project_id']} | {p.get('public_id')} | {(l.get('title') or l['target_id'])[:70]} | {l['match_basis']} | {l['confidence']:.2f} | {cat} | {ev} |")
    L.insert(3, "**Podsumowanie:** " + ", ".join(f"{k}: {v}" for k, v in stats.items()) + "\n")
    val = rows(conn, "SELECT project_id, validation FROM projects")
    issues = [(v["project_id"], x) for v in val for x in (unj(v["validation"], []) or []) if x["level"] in ("error", "warning")]
    if issues:
        L.append("\n## Wykryte błędy i niespójności w danych wejściowych\n")
        for pid, x in issues:
            L.append(f"- **{pid}** [{x['level']}] {x['field']}: {x['message']}")
    return "\n".join(L) + "\n"
