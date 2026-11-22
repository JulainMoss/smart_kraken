"""Przetwarzanie pakietu plików jednym kliknięciem (np. test jury): rekord projektu + opis nowej inwestycji
-> eksport rekordu projektu i analiza nowej inwestycji jako pliki tekstowe (Markdown, JSON, CSV, PDF z tekstem).

Bez zmian w kodzie: ten sam przebieg co w interfejsie (import -> łączenie -> wzbogacenie -> eksport; opis -> parametry -> raport)."""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import config, export, llm
from .db import j
from .docs import full_text, read_any
from .importer import enrich_project, import_table, project_record
from .query import understand
from .report import analyze
from .textutil import now_iso

Say = Callable[[str], None]


def results_root() -> Path:
    return config.path("uploads").parent / "wyniki"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:80]


def _chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge", "microsoft-edge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def print_pdf(url: str, out: Path, say: Say | None = None) -> bool:
    """PDF z tekstem (nie obraz) z widoku raportu w przeglądarce – jeśli na komputerze jest Chrome/Chromium/Edge."""
    exe = _chrome()
    if not exe:
        return False
    with tempfile.TemporaryDirectory() as prof:
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run", f"--user-data-dir={prof}",
               "--virtual-time-budget=20000", "--no-pdf-header-footer", f"--print-to-pdf={out}", url]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False)
        except Exception as e:  # PDF jest dodatkiem – Markdown/JSON/CSV powstają zawsze
            if say:
                say(f"PDF pominięty ({type(e).__name__})")
            return False
    return out.exists() and out.stat().st_size > 2000


def process(conn: sqlite3.Connection, project: tuple[str, bytes] | None, investment: tuple[str, bytes] | None,
            investment_text: str | None = None, external: bool = True, base_url: str | None = None,
            say: Say = print) -> dict[str, Any]:
    run_id = now_iso().replace(":", "").replace("-", "").replace("T", "-")[:15]
    out = results_root() / run_id
    out.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    result: dict[str, Any] = {"run": run_id, "dir": str(out), "files": files, "projects": [], "analysis": None}

    def add(path: Path, label: str, main: bool = False):
        files.append({"name": path.name, "size": path.stat().st_size, "label": label, "main": main})

    # 1) rekord projektu -> import, łączenie z BZP/TED/KIO/BIP/NIK, eksport
    if project:
        name, data = project
        say(f"Wczytuję rekord projektu: {name}")
        res = import_table(conn, name, data, progress=say)
        if not res["imported"]:
            raise ValueError(f"W pliku {name} nie znaleziono rekordów projektów (rozpoznane kolumny: {res.get('columns')}).")
        for i, pid in enumerate(res["imported"], 1):
            say(f"Projekt {pid}: łączenie ze źródłami publicznymi ({i}/{len(res['imported'])})")
            try:
                enrich_project(conn, pid, live=True, external=external, progress=say)
            except Exception as e:  # brak sieci – zostaje łączenie z lokalną bazą
                say(f"Uwaga: wzbogacanie online przerwane ({type(e).__name__}: {e}); użyto danych lokalnych.")
            conn.commit()
            rec = project_record(conn, pid)
            base = f"1_rekord_projektu_{_safe(pid)}"
            (out / f"{base}.md").write_text(export.project_markdown(rec), encoding="utf-8")
            (out / f"{base}.json").write_text(export.to_json(rec), encoding="utf-8")
            (out / f"{base}.csv").write_text("﻿" + export.project_csv(rec), encoding="utf-8")
            add(out / f"{base}.md", f"Eksport rekordu projektu {pid} (Markdown)", main=True)
            add(out / f"{base}.json", "ten sam rekord – JSON (pełne dane z dowodami)")
            add(out / f"{base}.csv", "ten sam rekord – CSV (Excel)")
            s = rec.get("summary") or rec.get("project", {}).get("summary") or {}
            result["projects"].append({"project_id": pid, "title": rec.get("project", {}).get("title"),
                                       "procedures": s.get("procedures"), "uncertain_links": s.get("uncertain_links"),
                                       "events": s.get("events")})
            say(f"Projekt {pid}: zapisano eksport rekordu.")

    # 2) opis nowej inwestycji -> parametry (model + reguły) -> analiza ryzyka
    if investment or (investment_text and investment_text.strip()):
        if investment:
            name, data = investment
            say(f"Wczytuję opis inwestycji: {name}")
            text = full_text(read_any(name, data)["pages"])
        else:
            text = investment_text.strip()
        if len(text) < 15:
            raise ValueError("Opis nowej inwestycji jest pusty lub nie udało się odczytać tekstu.")
        say(f"Odczytano {len(text):,} znaków opisu.".replace(",", " "))
        say("Odczytuję parametry inwestycji " + ("(model językowy + reguły)…" if llm.enabled() else "(reguły eksperckie)…"))
        q = understand(conn, text)
        if q.get("llm_info"):
            say(f"Model {q['llm_info']['model']} odczytał parametry w {q['llm_info']['seconds']} s.")
        for w in q.get("warnings", []):
            say("Uwaga: " + w)
        say("Szukam podobnych inwestycji i przygotowuję analizę…")
        rep = analyze(conn, q)
        conn.execute("INSERT OR REPLACE INTO analyses (analysis_id, created_at, title, query_text, params, result, status) VALUES (?,?,?,?,?,?,?)",
                     (rep["analysis_id"], rep["created_at"], rep["title"], text, j(q["params"]), j(rep), "final"))
        conn.commit()
        base = f"2_analiza_inwestycji_{_safe(rep['analysis_id'])}"
        (out / f"{base}.md").write_text(export.report_markdown(rep), encoding="utf-8")
        (out / f"{base}.json").write_text(export.to_json(rep), encoding="utf-8")
        (out / f"{base}.csv").write_text("﻿" + export.report_csv(rep), encoding="utf-8")
        add(out / f"{base}.md", f"Analiza nowej inwestycji: {rep['title']} (Markdown)", main=True)
        if base_url:
            say("Tworzę PDF raportu (z tekstem)…")
            pdf = out / f"{base}.pdf"
            if print_pdf(f"{base_url.rstrip('/')}/#/raport/{rep['analysis_id']}", pdf, say):
                add(pdf, "ta sama analiza – PDF z tekstem (wydruk raportu)")
        add(out / f"{base}.json", "ta sama analiza – JSON (pełne dane)")
        add(out / f"{base}.csv", "ta sama analiza – CSV (Excel)")
        result["analysis"] = {"analysis_id": rep["analysis_id"], "title": rep["title"], "n": rep["sample"]["n"],
                              "flags": len(rep["flags"]), "method": q.get("method")}
        say(f"Analiza gotowa: {rep['analysis_id']} (porównano z {rep['sample']['n']} inwestycjami).")

    if not files:
        raise ValueError("Dodaj co najmniej jeden plik: rekord projektu lub opis nowej inwestycji.")
    zpath = out / f"wyniki_riskminer_{run_id}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(out / f["name"], f["name"])
    result["zip"] = zpath.name
    say(f"Gotowe. Pliki wyników: {out}")
    return result
