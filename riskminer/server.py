"""Serwer aplikacji Risk Miner: API JSON + strona (web/). Wyłącznie biblioteka standardowa Pythona.

Uruchomienie:  python -m riskminer serve      (domyślnie http://127.0.0.1:8000)
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import re
import socketserver
import sys
import threading
import traceback
import urllib.parse
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Callable

from . import __version__, config, db, export, llm
from .build import aggregate_tenders
from .db import j, log_expert, one, rows, setting, unj
from .docs import full_text, read_any
from .importer import enrich_project, import_document, import_table, project_record, project_summary
from .query import apply_overrides, understand
from .report import analyze
from .textutil import now_iso, zunpack

WEB = Path(__file__).resolve().parent.parent / "web"
JOBS: dict[str, dict[str, Any]] = {}
MAX_UPLOAD = 60 * 1024 * 1024


def start_job(title: str, fn: Callable[[Callable[[str], None]], Any]) -> str:
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "title": title, "status": "running", "log": [], "result": None, "started": now_iso()}
    JOBS[jid] = job

    def say(msg: str) -> None:
        job["log"].append(f"{dt.datetime.now():%H:%M:%S} {msg}")
        job["log"] = job["log"][-200:]

    def run():
        try:
            job["result"] = fn(say)
            job["status"] = "done"
        except Exception as e:
            traceback.print_exc()
            job["status"] = "error"
            say(f"BŁĄD: {type(e).__name__}: {e}")
        job["finished"] = now_iso()
    threading.Thread(target=run, daemon=True).start()
    return jid


# ------------------------------------------------------------------------------------------------ obsługa API
def api_status(conn, q, body):
    c = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "version": __version__,
        "db": {"notices": c("SELECT COUNT(*) FROM notices"), "procedures": c("SELECT COUNT(*) FROM procedures"),
               "completed": c("SELECT COUNT(*) FROM procedures WHERE has_outcome=1"), "contracts": c("SELECT COUNT(*) FROM contracts"),
               "changes": c("SELECT COUNT(*) FROM changes"), "events": c("SELECT COUNT(*) FROM risk_events"),
               "events_pending": c("SELECT COUNT(*) FROM risk_events WHERE status='pending'"),
               "events_reviewed": c("SELECT COUNT(*) FROM risk_events WHERE status!='pending'"),
               "projects": c("SELECT COUNT(*) FROM projects"), "documents": c("SELECT COUNT(*) FROM documents"),
               "organizations": c("SELECT COUNT(*) FROM organizations"),
               "links_uncertain": c("SELECT COUNT(*) FROM project_links WHERE status='uncertain'"),
               "sectors": rows(conn, "SELECT sector, COUNT(*) n, SUM(has_outcome) completed FROM procedures GROUP BY sector ORDER BY n DESC"),
               "last_update": c("SELECT MAX(finished_at) FROM sources")},
        "llm": llm.status(),
        "monitor_unseen": c("SELECT COUNT(*) FROM monitor_events WHERE seen=0"),
    }


def api_understand(conn, q, body):
    text = (body.get("text") or "").strip()
    if len(text) < 15:
        raise ValueError("Opis jest za krótki – napisz kilka zdań o planowanej inwestycji.")
    return understand(conn, text, use_llm=body.get("use_llm", True))


def api_analyze(conn, q, body):
    text = (body.get("text") or "").strip()
    if len(text) < 15:
        raise ValueError("Opis jest za krótki – napisz kilka zdań o planowanej inwestycji.")
    qq = body.get("query") or understand(conn, text, use_llm=body.get("use_llm", True))
    qq["text"] = text
    qq = apply_overrides(qq, body.get("overrides"))
    rep = analyze(conn, qq, title=body.get("title"), use_llm=body.get("use_llm", True))
    conn.execute("INSERT OR REPLACE INTO analyses (analysis_id, created_at, title, query_text, params, result, status) VALUES (?,?,?,?,?,?,?)",
                 (rep["analysis_id"], rep["created_at"], rep["title"], text, j(qq["params"]), j(rep), "draft"))
    conn.commit()
    return rep


def api_analyses(conn, q, body):
    return rows(conn, "SELECT analysis_id, created_at, title, status, substr(query_text,1,200) AS excerpt FROM analyses ORDER BY created_at DESC LIMIT 200")


def api_analysis(conn, q, body, aid):
    r = one(conn, "SELECT result FROM analyses WHERE analysis_id=?", (aid,))
    if not r:
        raise KeyError("Nie znaleziono analizy")
    return unj(r["result"], {})


def api_projects(conn, q, body):
    out = rows(conn, "SELECT project_id, title, org_name, org_id, province, public_id_type, public_id, status, programme, project_value, subsector, "
                     "imported_at, enriched_at, summary, validation, source_file FROM projects ORDER BY project_id")
    for p in out:
        p["summary"] = unj(p.get("summary"), {})
        v = unj(p.get("validation"), [])
        p["validation_issues"] = sum(1 for x in v if x["level"] in ("error", "warning"))
        p.pop("validation", None)
    return out


def api_project(conn, q, body, pid):
    return project_record(conn, pid)


def api_procedure(conn, q, body, tid):
    p = one(conn, "SELECT * FROM procedures WHERE tender_id=?", (tid,))
    if not p:
        raise KeyError("Nie znaleziono postępowania")
    for k in ("features", "subsectors_extra", "risk_codes"):
        p[k] = unj(p.get(k), [])
    p["notices"] = rows(conn, "SELECT notice_number, notice_type, pub_date, title, url, fetched_at FROM notices WHERE tender_id=? ORDER BY pub_date", (tid,))
    p["contracts"] = rows(conn, "SELECT * FROM contracts WHERE tender_id=?", (tid,))
    p["changes"] = rows(conn, "SELECT * FROM changes WHERE tender_id=? ORDER BY change_date, change_id", (tid,))
    p["events"] = rows(conn, "SELECT e.*, c.name AS risk_name FROM risk_events e LEFT JOIN risk_catalog c ON c.code=e.risk_code WHERE tender_id=? ORDER BY confidence DESC", (tid,))
    p["organization"] = one(conn, "SELECT * FROM organizations WHERE org_key=?", (p.get("org_key"),))
    p["projects"] = rows(conn, "SELECT project_id, status, confidence FROM project_links WHERE target_id=?", (tid,))
    return p


def api_notice_text(conn, q, body, number):
    r = one(conn, "SELECT notice_number, notice_type, pub_date, title, url, data, text_z FROM notices WHERE notice_number=?", (number,))
    if not r:
        raise KeyError("Nie znaleziono ogłoszenia")
    d = unj(r["data"], {})
    text = zunpack(r["text_z"]) if r["text_z"] else (d.get("description") or "")
    return {"notice_number": r["notice_number"], "notice_type": r["notice_type"], "pub_date": r["pub_date"], "title": r["title"], "url": r["url"],
            "text": text, "fields": {k: v for k, v in d.items() if k not in ("org", "parts", "changes", "description") and v not in (None, "", [])},
            "org": d.get("org"), "parts": d.get("parts"), "changes": d.get("changes")}


def api_document(conn, q, body, doc_id):
    r = one(conn, "SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    if not r:
        raise KeyError("Nie znaleziono dokumentu")
    r["pages"] = unj(zunpack(r.pop("text_z")), [])
    r["events"] = rows(conn, "SELECT * FROM risk_events WHERE doc_id=?", (doc_id,))
    return r


def api_review(conn, q, body):
    kind = q.get("type", "events")
    status = q.get("status", "pending")
    limit = min(int(q.get("limit", 50)), 500)
    offset = int(q.get("offset", 0))
    if kind == "links":
        where = "WHERE l.status=?" if status != "all" else "WHERE 1=1"
        args = [status] if status != "all" else []
        items = rows(conn, f"""SELECT l.*, p.title AS project_title, p.org_name AS project_org, p.public_id, pr.org_name AS target_org, pr.nip AS target_nip,
            pr.year AS target_year, pr.contract_value AS target_value FROM project_links l JOIN projects p ON p.project_id=l.project_id
            LEFT JOIN procedures pr ON pr.tender_id=l.target_id {where} ORDER BY l.confidence ASC LIMIT ? OFFSET ?""", args + [limit, offset])
        for it in items:
            it["evidence"] = unj(it.get("evidence"), [])
        total = conn.execute(f"SELECT COUNT(*) FROM project_links l {where}", args).fetchone()[0]
        return {"items": items, "total": total}
    cond, args = [], []
    if status != "all":
        cond.append("e.status=?")
        args.append(status)
    if q.get("code"):
        cond.append("e.risk_code=?")
        args.append(q["code"])
    if q.get("scope") == "projects":
        cond.append("(e.project_id IS NOT NULL OR e.tender_id IN (SELECT target_id FROM project_links WHERE status!='rejected'))")
    if q.get("q"):
        cond.append("(e.quote LIKE ? OR p.title LIKE ?)")
        args += [f"%{q['q']}%", f"%{q['q']}%"]
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    order = "ORDER BY (e.project_id IS NULL AND e.tender_id NOT IN (SELECT target_id FROM project_links)) ASC, e.confidence ASC, e.event_id" \
        if q.get("order") != "recent" else "ORDER BY e.reviewed_at DESC"
    items = rows(conn, f"""SELECT e.*, c.name AS risk_name, p.title AS case_title, p.org_name AS case_org, p.year AS case_year
        FROM risk_events e LEFT JOIN risk_catalog c ON c.code=e.risk_code LEFT JOIN procedures p ON p.tender_id=e.tender_id
        {where} {order} LIMIT ? OFFSET ?""", args + [limit, offset])
    for it in items:
        for k in ("effect_types", "actions", "matched_terms"):
            it[k] = unj(it.get(k), [])
    total = conn.execute(f"SELECT COUNT(*) FROM risk_events e LEFT JOIN procedures p ON p.tender_id=e.tender_id {where}", args).fetchone()[0]
    return {"items": items, "total": total}


def api_event_decide(conn, q, body, eid):
    e = one(conn, "SELECT * FROM risk_events WHERE event_id=?", (eid,))
    if not e:
        raise KeyError("Nie znaleziono zdarzenia")
    action = body.get("action")
    reviewer = body.get("reviewer") or config.get()["expert"]["default_reviewer"]
    upd: dict[str, Any] = {"reviewer": reviewer, "reviewed_at": now_iso()}
    if action == "approve":
        upd["status"] = "approved"
    elif action == "reject":
        upd["status"] = "rejected"
    elif action == "correct":
        upd["status"] = "corrected"
        for k in ("risk_code", "cause", "effect_desc", "effect_time_days", "effect_cost_pln", "action_effectiveness", "event_type"):
            if k in body and body[k] not in (None, ""):
                upd[k] = body[k]
    elif action == "reset":
        upd["status"] = "pending"
    else:
        raise ValueError("Nieznana akcja")
    if "exemplary" in body:
        upd["exemplary"] = 1 if body["exemplary"] else 0
    if body.get("comment"):
        upd["expert_comment"] = body["comment"]
    conn.execute(f"UPDATE risk_events SET {', '.join(f'{k}=?' for k in upd)} WHERE event_id=?", list(upd.values()) + [eid])
    log_expert(conn, "zdarzenie", eid, action, {k: e.get(k) for k in upd}, upd, body.get("comment", ""), reviewer)
    if e.get("tender_id"):
        codes = sorted({r[0] for r in conn.execute("SELECT DISTINCT risk_code FROM risk_events WHERE tender_id=? AND status!='rejected'", (e["tender_id"],))})
        conn.execute("UPDATE procedures SET risk_codes=? WHERE tender_id=?", (j(codes), e["tender_id"]))
    conn.commit()
    return one(conn, "SELECT * FROM risk_events WHERE event_id=?", (eid,))


def api_link_decide(conn, q, body, lid):
    l = one(conn, "SELECT * FROM project_links WHERE link_id=?", (lid,))
    if not l:
        raise KeyError("Nie znaleziono powiązania")
    action = body.get("action")
    status = {"approve": "approved", "reject": "rejected", "reset": "uncertain"}.get(action)
    if not status:
        raise ValueError("Nieznana akcja")
    reviewer = body.get("reviewer") or config.get()["expert"]["default_reviewer"]
    conn.execute("UPDATE project_links SET status=?, reviewed_at=?, reviewer=? WHERE link_id=?", (status, now_iso(), reviewer, lid))
    log_expert(conn, "powiązanie", lid, action, {"status": l["status"]}, {"status": status}, body.get("comment", ""), reviewer)
    summ = project_summary(conn, l["project_id"])
    conn.execute("UPDATE projects SET summary=? WHERE project_id=?", (j(summ), l["project_id"]))
    conn.commit()
    return one(conn, "SELECT * FROM project_links WHERE link_id=?", (lid,))


def api_catalog(conn, q, body):
    out = rows(conn, "SELECT * FROM risk_catalog ORDER BY status='proposed', code")
    for r in out:
        for k in ("patterns", "mitigations", "clauses", "checklist"):
            r[k] = unj(r.get(k), [])
        r["events"] = conn.execute("SELECT COUNT(*) FROM risk_events WHERE risk_code=? AND status!='rejected'", (r["code"],)).fetchone()[0]
        r["approved"] = conn.execute("SELECT COUNT(*) FROM risk_events WHERE risk_code=? AND status IN ('approved','corrected')", (r["code"],)).fetchone()[0]
    return out


def api_catalog_save(conn, q, body):
    code = (body.get("code") or "").strip().upper()
    if not re.match(r"^[A-Z]{1,3}\d{2,3}$", code):
        raise ValueError("Kod kategorii w formacie np. R16 lub N04.")
    for p in body.get("patterns") or []:
        re.compile(p)
    before = one(conn, "SELECT * FROM risk_catalog WHERE code=?", (code,))
    rec = {"code": code, "name": body.get("name") or code, "status": body.get("status") or "active", "effects": body.get("effects") or "",
           "patterns": j(body.get("patterns") or []), "mitigations": j(body.get("mitigations") or []), "clauses": j(body.get("clauses") or []),
           "checklist": j(body.get("checklist") or []), "sector": body.get("sector") or "*",
           "proposed_by": (before or {}).get("proposed_by") or body.get("reviewer") or "ekspert", "updated_at": now_iso()}
    cols = list(rec)
    conn.execute(f"INSERT OR REPLACE INTO risk_catalog ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [rec[c] for c in cols])
    log_expert(conn, "katalog ryzyk", code, "edycja" if before else "dodanie", before, rec, body.get("comment", ""), body.get("reviewer") or "ekspert")
    conn.commit()
    return rec


def api_settings(conn, q, body):
    return {r["key"]: unj(r["value"]) for r in rows(conn, "SELECT key, value FROM settings")}


def api_settings_save(conn, q, body):
    for k, v in (body or {}).items():
        before = setting(conn, k)
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (k, j(v), now_iso()))
        log_expert(conn, "ustawienia", k, "zmiana", before, v)
    conn.commit()
    return api_settings(conn, q, body)


def api_exemplary(conn, q, body, tid):
    val = 1 if body.get("exemplary", True) else 0
    conn.execute("UPDATE procedures SET exemplary=?, expert_note=COALESCE(?, expert_note) WHERE tender_id=?", (val, body.get("note"), tid))
    log_expert(conn, "postępowanie", tid, "wzorcowe" if val else "niewzorcowe", None, {"exemplary": val, "note": body.get("note")})
    conn.commit()
    return {"tender_id": tid, "exemplary": val}


def api_expert_log(conn, q, body):
    return rows(conn, "SELECT * FROM expert_log ORDER BY log_id DESC LIMIT 300")


def api_sources(conn, q, body):
    return rows(conn, "SELECT * FROM sources ORDER BY source_id DESC LIMIT 300")


def api_jobs(conn, q, body, jid=None):
    if jid:
        if jid not in JOBS:
            raise KeyError("Nie ma takiego zadania")
        return JOBS[jid]
    return sorted(JOBS.values(), key=lambda x: x["started"], reverse=True)[:30]


def api_enrich(conn, q, body, pid):
    live = body.get("live", True)

    def run(say):
        c = db.connect()
        return enrich_project(c, pid, live=live, external=body.get("external", True), progress=say)
    return {"job": start_job(f"Wzbogacanie projektu {pid}", run)}


def api_enrich_all(conn, q, body):
    ids = [r[0] for r in conn.execute("SELECT project_id FROM projects ORDER BY project_id")]

    def run(say):
        c = db.connect()
        out = {}
        for i, pid in enumerate(ids, 1):
            say(f"[{i}/{len(ids)}] {pid}")
            try:
                out[pid] = enrich_project(c, pid, live=body.get("live", True), external=body.get("external", False), progress=lambda m: say("   " + m))
            except Exception as e:
                say(f"   błąd: {e}")
        return {"projects": len(out)}
    return {"job": start_job("Wzbogacanie wszystkich projektów", run)}


def api_reanalyze(conn, q, body):
    """Ponowna analiza (np. po zmianie reguł katalogu lub nowych dokumentach) – decyzje eksperta są zachowywane."""
    scope = body.get("scope", "projects")
    if scope == "all":
        tids = None
    else:
        tids = [r[0] for r in conn.execute("SELECT DISTINCT target_id FROM project_links WHERE target_type='procedure' AND status!='rejected'")]

    def run(say):
        c = db.connect()
        say("Ponowna analiza postępowań z aktualnymi regułami…")
        n = aggregate_tenders(c, tids, progress_every=2000)
        docs = [r[0] for r in c.execute("SELECT doc_id FROM documents")]
        from .importer import analyze_document
        for d in docs:
            analyze_document(c, d)
        for (pid,) in c.execute("SELECT project_id FROM projects").fetchall():
            c.execute("UPDATE projects SET summary=? WHERE project_id=?", (j(project_summary(c, pid)), pid))
        c.commit()
        say(f"Przeanalizowano {n} postępowań i {len(docs)} dokumentów.")
        return {"procedures": n, "documents": len(docs)}
    return {"job": start_job("Ponowna analiza", run)}


def api_monitor(conn, q, body):
    from .sources import bzp_api

    def run(say):
        c = db.connect()
        tids = [r[0] for r in c.execute("""SELECT DISTINCT l.target_id FROM project_links l JOIN procedures p ON p.tender_id=l.target_id
            WHERE l.target_type='procedure' AND l.status!='rejected' AND p.status!='zakończone'""")]
        say(f"Monitoruję {len(tids)} trwających postępowań powiązanych z projektami…")
        evs = bzp_api.monitor_tenders(c, tids)
        for e in evs:
            pid = c.execute("SELECT project_id FROM project_links WHERE target_id=? LIMIT 1", (e["tender_id"],)).fetchone()
            c.execute("INSERT INTO monitor_events (ts, project_id, tender_id, notice_number, kind, summary) VALUES (?,?,?,?,?,?)",
                      (now_iso(), pid[0] if pid else None, e["tender_id"], e["notice_number"], e["kind"], e.get("title")))
            say(f"NOWE: {e['kind']} {e['notice_number']}")
        c.commit()
        say(f"Zakończono: {len(evs)} nowych ogłoszeń.")
        return {"new": len(evs)}
    return {"job": start_job("Monitorowanie trwających inwestycji", run)}


def api_extras(conn, q, body):
    from .sources.lake import ingest_lake

    def run(say):
        import builtins
        c = db.connect()
        orig = builtins.print
        builtins.print = lambda *a, **k: say(" ".join(str(x) for x in a))
        try:
            ingest_lake(c, config.path("data_lake"))
        finally:
            builtins.print = orig
        return {"ok": True}
    return {"job": start_job("Wczytanie źródeł z data_lake (KIO, TED, BZP wielosektorowe, KPOŚK)", run)}


def api_monitor_events(conn, q, body):
    return rows(conn, "SELECT * FROM monitor_events ORDER BY id DESC LIMIT 100")


def api_search_cases(conn, q, body):
    """Przeglądarka bazy przypadków (filtrowanie, eksport)."""
    cond, args = ["1=1"], []
    if q.get("q"):
        from .search import _fts_query
        fq = _fts_query(q["q"])
        if fq:
            cond.append("tender_id IN (SELECT tender_id FROM procedures_fts WHERE procedures_fts MATCH ? LIMIT 3000)")
            args.append(fq)
    for k in ("subsector", "province", "sector", "status"):
        if q.get(k):
            cond.append(f"{k}=?")
            args.append(q[k])
    if q.get("delayed") in ("0", "1"):
        cond.append("delayed=?")
        args.append(int(q["delayed"]))
    if q.get("risk"):
        cond.append("risk_codes LIKE ?")
        args.append(f'%"{q["risk"]}"%')
    if q.get("exemplary") == "1":
        cond.append("exemplary=1")
    limit = min(int(q.get("limit", 50)), 2000)
    where = " AND ".join(cond)
    items = rows(conn, f"""SELECT tender_id, title, org_name, nip, province, year, sector, subsector, status, contract_value, paid_value, cost_change_pct,
        delayed, delay_days, n_changes, risk_codes, exemplary, length_km FROM procedures WHERE {where} ORDER BY year DESC, contract_value DESC LIMIT ?""", args + [limit])
    for it in items:
        it["risk_codes"] = [c for c in unj(it.get("risk_codes"), []) if c != "BRAK"]
    total = conn.execute(f"SELECT COUNT(*) FROM procedures WHERE {where}", args).fetchone()[0]
    return {"items": items, "total": total}


def handle_upload(conn, q, data: bytes, filename: str):
    """Jedno okno importu: tabela projektów -> import + automatyczne wzbogacenie; dokument -> do projektu albo do analizy."""
    purpose = q.get("purpose", "auto")
    ext = Path(filename).suffix.lower()
    if purpose == "analysis":
        parsed = read_any(filename, data)
        text = full_text(parsed["pages"])
        return {"kind": "text", "filename": filename, "text": text[:200000], "pages": len(parsed["pages"]), "chars": len(text)}
    if ext in (".xlsx", ".xls", ".csv", ".tsv", ".xlsm") and purpose in ("auto", "table"):
        res = import_table(conn, filename, data)
        ids = res["imported"]
        if q.get("enrich", "1") == "1" and ids:
            def run(say):
                c = db.connect()
                for i, pid in enumerate(ids, 1):
                    say(f"[{i}/{len(ids)}] Wzbogacam {pid}…")
                    try:
                        enrich_project(c, pid, live=True, external=q.get("external", "1") == "1", progress=lambda m: say("   " + m))
                    except Exception as e:
                        say(f"   błąd: {e}")
                return {"projects": ids}
            res["job"] = start_job(f"Import i wzbogacenie {len(ids)} projektów z {filename}", run)
        res["kind"] = "table"
        return res
    res = import_document(conn, filename, data, project_id=q.get("project_id") or None, tender_id=q.get("tender_id") or None,
                          uploads_dir=config.path("uploads"))
    res["kind"] = "document"
    return res


ROUTES: list[tuple[str, str, Callable]] = [
    ("GET", r"/api/status", api_status),
    ("POST", r"/api/understand", api_understand),
    ("POST", r"/api/analyze", api_analyze),
    ("GET", r"/api/analyses", api_analyses),
    ("GET", r"/api/analyses/([\w-]+)", api_analysis),
    ("GET", r"/api/projects", api_projects),
    ("GET", r"/api/projects/([\w.-]+)", api_project),
    ("POST", r"/api/projects/([\w.-]+)/enrich", api_enrich),
    ("POST", r"/api/enrich_all", api_enrich_all),
    ("GET", r"/api/procedures/([\w.-]+)", api_procedure),
    ("POST", r"/api/procedures/([\w.-]+)/exemplary", api_exemplary),
    ("GET", r"/api/notice", None),  # obsługiwane osobno (numer zawiera '/')
    ("GET", r"/api/documents/([\w.-]+)", api_document),
    ("GET", r"/api/review", api_review),
    ("POST", r"/api/events/(\d+)", api_event_decide),
    ("POST", r"/api/links/(\d+)", api_link_decide),
    ("GET", r"/api/catalog", api_catalog),
    ("POST", r"/api/catalog", api_catalog_save),
    ("GET", r"/api/settings", api_settings),
    ("POST", r"/api/settings", api_settings_save),
    ("GET", r"/api/expert_log", api_expert_log),
    ("GET", r"/api/sources", api_sources),
    ("GET", r"/api/jobs", api_jobs),
    ("GET", r"/api/jobs/(\w+)", api_jobs),
    ("POST", r"/api/reanalyze", api_reanalyze),
    ("POST", r"/api/monitor", api_monitor),
    ("GET", r"/api/monitor", api_monitor_events),
    ("POST", r"/api/extras", api_extras),
    ("GET", r"/api/cases", api_search_cases),
]


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "RiskMiner/" + __version__

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any):
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _file(self, name: str, content: str | bytes, ctype: str):
        data = content.encode("utf-8") if isinstance(content, str) else content
        self._send(200, data, ctype, {"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        parts = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parts.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query).items()}
        conn = db.get()
        try:
            if path.startswith("/api/"):
                return self._api(method, path, q, conn)
            return self._static(path)
        except KeyError as e:
            self._json(404, {"error": str(e).strip("'")})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _api(self, method, path, q, conn):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._json(413, {"error": "Plik jest za duży (maks. 60 MB)."})
        raw = self.rfile.read(length) if length else b""
        if method == "POST" and path == "/api/upload":
            name = urllib.parse.unquote(self.headers.get("X-Filename") or "plik.bin")
            return self._json(200, handle_upload(conn, q, raw, name))
        if path == "/api/notice":
            return self._json(200, api_notice_text(conn, q, {}, q.get("n", "")))
        # eksporty plików
        m = re.fullmatch(r"/api/analyses/([\w-]+)/export", path)
        if m:
            rep = api_analysis(conn, q, {}, m.group(1))
            fmt = q.get("format", "md")
            base = f"raport_{m.group(1)}"
            if fmt == "json":
                return self._file(base + ".json", export.to_json(rep), "application/json; charset=utf-8")
            if fmt == "csv":
                return self._file(base + ".csv", "﻿" + export.report_csv(rep), "text/csv; charset=utf-8")
            return self._file(base + ".md", export.report_markdown(rep), "text/markdown; charset=utf-8")
        m = re.fullmatch(r"/api/projects/([\w.-]+)/export", path)
        if m:
            rec = project_record(conn, m.group(1))
            fmt = q.get("format", "json")
            base = f"projekt_{m.group(1)}"
            if fmt == "md":
                return self._file(base + ".md", export.project_markdown(rec), "text/markdown; charset=utf-8")
            if fmt == "csv":
                return self._file(base + ".csv", "﻿" + export.project_csv(rec), "text/csv; charset=utf-8")
            return self._file(base + ".json", export.to_json(rec), "application/json; charset=utf-8")
        if path == "/api/export/projects":
            recs = [project_record(conn, r[0]) for r in conn.execute("SELECT project_id FROM projects ORDER BY project_id")]
            return self._file("projekty_riskminer.json", export.to_json(recs), "application/json; charset=utf-8")
        if path == "/api/export/events":
            evs = rows(
                conn, "SELECT e.event_id, e.tender_id, e.project_id, e.event_type, e.risk_code, c.name AS risk_name, e.cause, e.effect_time_days, e.effect_cost_pln, "
                      "e.actions, e.action_effectiveness, e.quote, e.source_ref, e.source_url, e.locator, e.confidence, e.method, e.status, e.reviewer, e.reviewed_at "
                      "FROM risk_events e LEFT JOIN risk_catalog c ON c.code=e.risk_code " +
                      ("WHERE e.status=? " if q.get("status") else "") + "ORDER BY e.event_id", ([q["status"]] if q.get("status") else []))
            return self._file("zdarzenia_ryzyka.csv", "﻿" + export.rows_csv(evs), "text/csv; charset=utf-8")
        if path == "/api/export/cases":
            items = api_search_cases(conn, {**q, "limit": q.get("limit", "2000")}, {})["items"]
            return self._file("baza_przypadkow.csv", "﻿" + export.rows_csv(items), "text/csv; charset=utf-8")
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return self._json(400, {"error": "Niepoprawny JSON"})
        for meth, pat, fn in ROUTES:
            if meth != method or fn is None:
                continue
            m = re.fullmatch(pat, path)
            if m:
                return self._json(200, fn(conn, q, body, *m.groups()))
        return self._json(404, {"error": "Nieznany adres API"})

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        target = (WEB / path.lstrip("/")).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.is_file():
            target = WEB / "index.html"   # aplikacja jednostronicowa
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json"}.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host: str | None = None, port: int | None = None, open_browser: bool | None = None) -> None:
    cfg = config.get()
    host = host or cfg["server"]["host"]
    port = int(port or cfg["server"]["port"])
    db.DB_PATH = config.path("db_path")
    conn = db.connect()
    db.seed_knowledge(conn)
    srv = Server((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    n = conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0]
    print(f"Risk Miner {__version__} działa: {url}")
    print(f"Baza: {db.DB_PATH} ({n:,} postępowań)".replace(",", " "))
    if n == 0:
        print("UWAGA: baza jest pusta – zbuduj ją poleceniem:  python -m riskminer build")
    st = llm.status()
    print(f"Model językowy: {st.get('provider')} {st.get('model') if st.get('enabled') else '(wyłączony – działają reguły i statystyka)'}")
    if open_browser if open_browser is not None else cfg["server"].get("open_browser", True):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymano.")
