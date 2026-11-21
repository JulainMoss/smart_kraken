"""Baza danych Risk Minera (SQLite, plik w data/). Interfejs gotowy do przeniesienia na PostgreSQL.

Model (zgodnie z wymaganiami MFiPR) rozdziela:
  projects (przedsięwzięcia) ─< project_links >─ procedures (postępowania, tenderId/OCID)
  procedures ─< notices (ogłoszenia BZP/TED)  ─< contracts (umowy/części) ─< changes (zmiany umów)
  risk_events (zdarzenia ryzyka z dowodem: ogłoszenie/dokument + cytat + sekcja/strona)
  documents (pliki i strony WWW: BIP, NIK, KIO, wgrane przez użytkownika) + sources (rejestr pobrań)
  risk_catalog, subsectors, sectors, settings (wiedza ekspercka edytowalna), expert_log (historia decyzji)
  analyses (zapisane analizy nowych inwestycji), monitor_events (moduł monitorowania)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from . import catalog
from .textutil import now_iso

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sectors (
  code TEXT PRIMARY KEY, name TEXT, status TEXT, cpv_prefixes TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS subsectors (
  code TEXT PRIMARY KEY, sector TEXT, name TEXT, scope TEXT, patterns TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS risk_catalog (
  code TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT DEFAULT 'active',   -- active | proposed | rejected
  effects TEXT, patterns TEXT, mitigations TEXT, clauses TEXT, checklist TEXT,
  sector TEXT DEFAULT '*', proposed_by TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS organizations (
  org_key TEXT PRIMARY KEY, nip TEXT, regon TEXT, name TEXT, city TEXT, province TEXT, street TEXT, postcode TEXT,
  email TEXT, phone TEXT, www TEXT, org_type TEXT, nuts TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS procedures (
  tender_id TEXT PRIMARY KEY,           -- identyfikator postępowania (OCID) z BZP lub TED
  source_system TEXT DEFAULT 'BZP',
  sector TEXT, subsector TEXT, subsectors_extra TEXT, works_type TEXT, design_build INTEGER,
  title TEXT, description TEXT, org_key TEXT, org_name TEXT, nip TEXT, province TEXT, city TEXT,
  cpv_main TEXT, cpv_all TEXT, estimated_value_net REAL, eu_funded INTEGER, tryb TEXT, platform_url TEXT,
  planned_days INTEGER, period_txt TEXT, changes_foreseen INTEGER, price_weight REAL,
  first_pub_date TEXT, last_pub_date TEXT, year INTEGER,
  length_km REAL, connections INTEGER, features TEXT,
  n_notices INTEGER DEFAULT 0, n_contracts INTEGER DEFAULT 0, n_changes INTEGER DEFAULT 0,
  offers_avg REAL, cancelled_parts INTEGER DEFAULT 0, parts INTEGER DEFAULT 0,
  contract_value REAL, paid_value REAL, cost_change_pct REAL,
  actual_days INTEGER, delay_days INTEGER, delayed INTEGER, improper INTEGER, not_executed INTEGER,
  has_outcome INTEGER DEFAULT 0, status TEXT,     -- ogłoszone | udzielone | zakończone | unieważnione
  risk_codes TEXT, exemplary INTEGER DEFAULT 0, expert_note TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_proc_nip ON procedures(nip);
CREATE INDEX IF NOT EXISTS ix_proc_sub ON procedures(subsector);
CREATE INDEX IF NOT EXISTS ix_proc_outcome ON procedures(has_outcome);

CREATE VIRTUAL TABLE IF NOT EXISTS procedures_fts USING fts5(
  tender_id UNINDEXED, title_s, desc_s, changes_s, tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS fts_map (tender_id TEXT PRIMARY KEY, fts_rowid INTEGER);

CREATE TABLE IF NOT EXISTS notices (
  notice_number TEXT PRIMARY KEY, tender_id TEXT, notice_type TEXT, pub_date TEXT, title TEXT,
  object_id TEXT, url TEXT, source_system TEXT DEFAULT 'BZP', data TEXT, text_z BLOB, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_notice_tender ON notices(tender_id);

CREATE TABLE IF NOT EXISTS contracts (
  contract_id TEXT PRIMARY KEY, tender_id TEXT, part INTEGER, contractor TEXT, contractor_nip TEXT,
  contract_date TEXT, period_txt TEXT, planned_days INTEGER, contract_value REAL,
  result_notice TEXT, perf_notice TEXT, offers INTEGER, min_price REAL, max_price REAL, estimated_value_net REAL,
  cancelled INTEGER DEFAULT 0, cancel_reason TEXT,
  executed INTEGER, completion_date TEXT, on_time INTEGER, properly INTEGER, changes_count INTEGER,
  paid_value REAL, cost_change_pct REAL, actual_days INTEGER, delay_days INTEGER, additional_info TEXT
);
CREATE INDEX IF NOT EXISTS ix_contract_tender ON contracts(tender_id);

CREATE TABLE IF NOT EXISTS changes (
  change_id INTEGER PRIMARY KEY AUTOINCREMENT, tender_id TEXT, contract_id TEXT, source_notice TEXT, source_type TEXT,
  section TEXT, change_no INTEGER, change_date TEXT, legal_basis TEXT, basis_label TEXT, reason TEXT, description TEXT,
  value REAL, price_increase INTEGER, effect_types TEXT, time_ext_days INTEGER, actions TEXT
);
CREATE INDEX IF NOT EXISTS ix_change_tender ON changes(tender_id);

CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY, project_id TEXT, tender_id TEXT, source_system TEXT, doc_type TEXT, title TEXT,
  url TEXT, local_path TEXT, fetched_at TEXT, sha256 TEXT, mime TEXT, pages INTEGER, chars INTEGER,
  text_z BLOB, meta TEXT, status TEXT DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS ix_doc_project ON documents(project_id);

CREATE TABLE IF NOT EXISTS risk_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  tender_id TEXT, project_id TEXT, change_id INTEGER, doc_id TEXT,
  event_type TEXT,                      -- udokumentowany fakt: zmiana terminu, wykonanie po terminie, unieważnienie...
  risk_code TEXT, cause TEXT, effect_types TEXT, effect_time_days INTEGER, effect_cost_pln REAL, effect_desc TEXT,
  actions TEXT, action_effectiveness TEXT,
  quote TEXT, source_system TEXT, source_ref TEXT, source_url TEXT, locator TEXT,  -- sekcja ogłoszenia / strona dokumentu
  matched_terms TEXT, confidence REAL, method TEXT,
  status TEXT DEFAULT 'pending',        -- pending | approved | rejected | corrected
  reviewer TEXT, reviewed_at TEXT, expert_comment TEXT, exemplary INTEGER DEFAULT 0, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_tender ON risk_events(tender_id);
CREATE INDEX IF NOT EXISTS ix_event_status ON risk_events(status);
CREATE INDEX IF NOT EXISTS ix_event_project ON risk_events(project_id);
CREATE INDEX IF NOT EXISTS ix_event_doc ON risk_events(doc_id);
CREATE INDEX IF NOT EXISTS ix_event_code ON risk_events(risk_code);

CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY, title TEXT, org_name TEXT, org_id_type TEXT, org_id TEXT, org_type TEXT,
  province TEXT, teryt TEXT, city TEXT, scope TEXT, public_id_type TEXT, public_id TEXT, public_id_norm TEXT,
  theme TEXT, order_type TEXT, legal_regime TEXT, currency TEXT, status TEXT, record_source TEXT, record_date TEXT,
  record_version TEXT, annotation TEXT, project_value REAL, grant_value REAL, programme TEXT,
  sector TEXT, subsector TEXT, source_file TEXT, raw TEXT, validation TEXT, imported_at TEXT, enriched_at TEXT, summary TEXT
);

CREATE TABLE IF NOT EXISTS project_links (
  link_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, target_type TEXT, target_id TEXT, title TEXT,
  source_system TEXT, url TEXT, match_basis TEXT, confidence REAL, evidence TEXT,
  status TEXT DEFAULT 'auto',           -- auto (pewne) | uncertain | approved | rejected
  created_at TEXT, reviewed_at TEXT, reviewer TEXT,
  UNIQUE(project_id, target_type, target_id)
);

CREATE TABLE IF NOT EXISTS sources (
  source_id INTEGER PRIMARY KEY AUTOINCREMENT, source_system TEXT, operation TEXT, url TEXT, http_status INTEGER,
  records INTEGER, sha256 TEXT, local_path TEXT, started_at TEXT, finished_at TEXT, meta TEXT
);

CREATE TABLE IF NOT EXISTS expert_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, reviewer TEXT, entity_type TEXT, entity_id TEXT,
  action TEXT, before TEXT, after TEXT, comment TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
  analysis_id TEXT PRIMARY KEY, created_at TEXT, title TEXT, query_text TEXT, params TEXT, result TEXT, status TEXT DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS monitor_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, project_id TEXT, tender_id TEXT, notice_number TEXT,
  kind TEXT, summary TEXT, seen INTEGER DEFAULT 0
);
"""

_local = threading.local()
DB_PATH: Path | None = None


def default_db_path() -> Path:
    env = os.environ.get("RISKMINER_DB")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "riskminer.sqlite"


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH or default_db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get() -> sqlite3.Connection:
    """Połączenie per-wątek (serwer HTTP jest wielowątkowy)."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = connect()
        _local.conn = c
    return c


def j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def unj(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


def rows(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def seed_knowledge(conn: sqlite3.Connection, overwrite: bool = False) -> None:
    """Wgrywa wiedzę ekspercką (katalog ryzyk, podsektory, ustawienia) – nie nadpisuje zmian eksperta."""
    ts = now_iso()
    verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
    for s in catalog.SECTORS:
        conn.execute(f"{verb} INTO sectors VALUES (?,?,?,?,?)", (s["code"], s["name"], s["status"], j(s["cpv_prefixes"]), ts))
    for s in catalog.SUBSECTORS:
        conn.execute(f"{verb} INTO subsectors VALUES (?,?,?,?,?,?)", (s["code"], s["sector"], s["name"], s["scope"], j(s["patterns"]), ts))
    for r in catalog.RISKS:
        conn.execute(f"{verb} INTO risk_catalog (code,name,status,effects,patterns,mitigations,clauses,checklist,sector,proposed_by,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (r["code"], r["name"], r["status"], r["effects"], j(r["patterns"]), j(r["mitigations"]), j(r["clauses"]),
                      j(r["checklist"]), "*", "system" if r["status"] == "proposed" else "MFiPR", ts))
    for k, v in catalog.DEFAULT_SETTINGS.items():
        conn.execute(f"{verb} INTO settings VALUES (?,?,?)", (k, j(v), ts))
    conn.execute(f"{verb} INTO settings VALUES (?,?,?)", ("checklist", j(catalog.CHECKLIST), ts))
    conn.execute(f"{verb} INTO settings VALUES (?,?,?)", ("features", j(catalog.FEATURES), ts))
    conn.commit()


def setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return unj(r[0], default) if r else default


def log_expert(conn: sqlite3.Connection, entity_type: str, entity_id: str, action: str, before: Any = None,
               after: Any = None, comment: str = "", reviewer: str = "ekspert") -> None:
    conn.execute("INSERT INTO expert_log (ts,reviewer,entity_type,entity_id,action,before,after,comment) VALUES (?,?,?,?,?,?,?,?)",
                 (now_iso(), reviewer, entity_type, str(entity_id), action, j(before) if before is not None else None,
                  j(after) if after is not None else None, comment))


def log_source(conn: sqlite3.Connection, system: str, operation: str, url: str, status: int | None = None,
               records: int | None = None, sha256: str | None = None, local_path: str | None = None,
               started_at: str | None = None, meta: Any = None) -> None:
    conn.execute("INSERT INTO sources (source_system,operation,url,http_status,records,sha256,local_path,started_at,finished_at,meta) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (system, operation, url, status, records, sha256, local_path, started_at or now_iso(), now_iso(), j(meta or {})))
