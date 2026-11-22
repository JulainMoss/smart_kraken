"""Risk Miner – wiersz poleceń.

  python -m riskminer serve                         uruchom aplikację (http://127.0.0.1:8000)
  python -m riskminer build [--jsonl PLIK]          zbuduj bazę z kopii ogłoszeń BZP (JSONL z API e-Zamówień)
  python -m riskminer reanalyze                     ponowna analiza (po zmianie reguł) bez ponownego pobierania
  python -m riskminer import PLIK.xlsx              import projektów + automatyczne wzbogacenie (BZP, TED, KIO, BIP)
  python -m riskminer analyze OPIS.docx|txt|pdf     analiza nowej inwestycji -> raport (md/json/csv)
  python -m riskminer export-project RM-2026-001    eksport rekordu projektu (json/md/csv)
  python -m riskminer test-run --project P.xlsx --investment OPIS.docx --out wyniki/
                                                    pełna ścieżka testowa jury: import+wzbogacenie+eksport i analiza
  python -m riskminer extras                        wczytaj dodatkowe źródła z data_lake (KIO, TED, BZP wielosektorowe)
  python -m riskminer link-report                   raport z testu łączenia (poprawne / niepewne / błędne)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, db, export


def _conn():
    db.DB_PATH = config.path("db_path")
    c = db.connect()
    db.seed_knowledge(c)
    return c


def cmd_build(a):
    from .build import build_from_mirror
    c = _conn()
    build_from_mirror(c, Path(a.jsonl) if a.jsonl else config.path("bzp_mirror_jsonl"), limit=a.limit)
    print("Baza gotowa:", db.DB_PATH)


def cmd_reanalyze(a):
    from .build import aggregate_tenders
    c = _conn()
    n = aggregate_tenders(c)
    print(f"Przeanalizowano ponownie {n} postępowań.")


def cmd_import(a):
    from .importer import enrich_project, import_table
    c = _conn()
    data = Path(a.file).read_bytes()
    res = import_table(c, Path(a.file).name, data, progress=print)
    print(f"Zaimportowano {len(res['imported'])} projektów; rozpoznane kolumny: {res['columns']}")
    if not a.no_enrich:
        for pid in res["imported"]:
            print(f"== Wzbogacam {pid}")
            enrich_project(c, pid, live=not a.offline, external=not a.no_external, progress=lambda m: print("  ", m))
    return res


def _read_text(path_or_text: str) -> str:
    p = Path(path_or_text)
    if p.exists():
        from .docs import full_text, read_any
        return full_text(read_any(p.name, p.read_bytes())["pages"])
    return path_or_text


def cmd_analyze(a):
    from .query import understand
    from .report import analyze
    c = _conn()
    text = _read_text(a.input)
    q = understand(c, text)
    rep = analyze(c, q, title=a.title)
    c.execute("INSERT OR REPLACE INTO analyses (analysis_id, created_at, title, query_text, params, result) VALUES (?,?,?,?,?,?)",
              (rep["analysis_id"], rep["created_at"], rep["title"], text, db.j(q["params"]), db.j(rep)))
    c.commit()
    out = Path(a.out) if a.out else None
    md = export.report_markdown(rep)
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{rep['analysis_id']}.md").write_text(md, encoding="utf-8")
        (out / f"{rep['analysis_id']}.json").write_text(export.to_json(rep), encoding="utf-8")
        (out / f"{rep['analysis_id']}.csv").write_text("﻿" + export.report_csv(rep), encoding="utf-8")
        print(f"Zapisano raport w {out}/ ({rep['analysis_id']}.md/.json/.csv)")
    else:
        print(md)
    return rep


def cmd_export_project(a):
    from .importer import project_record
    c = _conn()
    rec = project_record(c, a.project_id)
    out = Path(a.out or ".")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"projekt_{a.project_id}.json").write_text(export.to_json(rec), encoding="utf-8")
    (out / f"projekt_{a.project_id}.md").write_text(export.project_markdown(rec), encoding="utf-8")
    (out / f"projekt_{a.project_id}.csv").write_text("﻿" + export.project_csv(rec), encoding="utf-8")
    print(f"Zapisano rekord projektu w {out}/projekt_{a.project_id}.json/.md/.csv")


def cmd_test_run(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.project:
        res = cmd_import(argparse.Namespace(file=a.project, no_enrich=False, offline=a.offline, no_external=False))
        for pid in res["imported"]:
            cmd_export_project(argparse.Namespace(project_id=pid, out=str(out)))
    if a.investment:
        cmd_analyze(argparse.Namespace(input=a.investment, title=None, out=str(out)))
    print(f"\nWyniki testu w katalogu: {out.resolve()}")


def cmd_auto_approve(a):
    from .expert_policy import auto_approve, policy
    c = _conn()
    n = auto_approve(c)
    print(f"Polityka eksperta {policy(c)}: zatwierdzono {n} zdarzeń.")


def cmd_extras(a):
    from .sources.lake import ingest_lake
    c = _conn()
    ingest_lake(c, config.path("data_lake"))


def cmd_link_report(a):
    from .linkreport import link_report
    c = _conn()
    md = link_report(c)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
        print("Zapisano", a.out)
    else:
        print(md)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m riskminer", description="Risk Miner – inteligentna baza doświadczeń inwestycji publicznych")
    ap.add_argument("--config", help="plik konfiguracji (domyślnie config.toml)")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="uruchom aplikację WWW")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")
    s = sub.add_parser("build", help="zbuduj bazę z kopii ogłoszeń BZP")
    s.add_argument("--jsonl")
    s.add_argument("--limit", type=int)
    sub.add_parser("reanalyze", help="ponowna analiza zdarzeń/regułami")
    s = sub.add_parser("import", help="import tabeli projektów")
    s.add_argument("file")
    s.add_argument("--no-enrich", action="store_true")
    s.add_argument("--no-external", action="store_true", help="bez TED/KIO/BIP/NIK")
    s.add_argument("--offline", action="store_true", help="tylko lokalna baza")
    s = sub.add_parser("analyze", help="analiza nowej inwestycji")
    s.add_argument("input", help="plik (docx/pdf/txt) lub tekst")
    s.add_argument("--title")
    s.add_argument("--out", help="katalog wyników")
    s = sub.add_parser("export-project")
    s.add_argument("project_id")
    s.add_argument("--out")
    s = sub.add_parser("test-run", help="ścieżka testowa jury")
    s.add_argument("--project")
    s.add_argument("--investment")
    s.add_argument("--out", default="wyniki_testu")
    s.add_argument("--offline", action="store_true")
    sub.add_parser("extras", help="wczytaj dodatkowe źródła z data_lake")
    sub.add_parser("auto-approve", help="polityka eksperta: zatwierdź zdarzenia o wysokim prawdopodobieństwie")
    s = sub.add_parser("link-report")
    s.add_argument("--out")
    a = ap.parse_args(argv)
    if a.config:
        config.load(Path(a.config))
    if a.cmd in (None, "serve"):
        from .server import serve
        serve(getattr(a, "host", None), getattr(a, "port", None), False if getattr(a, "no_browser", False) else None)
        return
    {"build": cmd_build, "reanalyze": cmd_reanalyze, "import": cmd_import, "analyze": cmd_analyze, "export-project": cmd_export_project,
     "test-run": cmd_test_run, "extras": cmd_extras, "link-report": cmd_link_report,
     "auto-approve": cmd_auto_approve}[a.cmd](a)


if __name__ == "__main__":
    main()
