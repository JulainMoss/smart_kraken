"""Publiczny odczyt BZP / e-Zamówień (API mo-board/v1/notice). Domyślnie kopia hackathonowa bzp.cytr.us
(ten sam interfejs, bez limitu), awaryjnie produkcja ezamowienia.gov.pl (limit 30 zapytań/min – szanujemy go)."""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from typing import Any

from .. import config
from ..build import aggregate_tenders, ingest_records
from ..db import log_source
from ..textutil import now_iso
from . import get_json

TYPES = ("ContractNotice", "TenderResultNotice", "AgreementUpdateNotice", "ContractPerformingNotice")
_last_call = [0.0]


def _bases() -> list[str]:
    s = config.get()["sources"]
    return [b for b in (s.get("bzp_api"), s.get("bzp_api_fallback")) if b]


def query(params: dict[str, Any], page_size: int = 100) -> list[dict]:
    """Zapytanie do API (z ponowieniem na zapasowym adresie)."""
    err = None
    for base in _bases():
        url = base.rstrip("/") + "/mo-board/api/v1/notice"
        p = dict(params)
        p.setdefault("PageSize", page_size)
        if "ezamowienia.gov.pl" in base:
            p.setdefault("PublicationDateFrom", "2021-01-01T00:00:00")
            p.setdefault("PublicationDateTo", dt.date.today().isoformat() + "T23:59:59")
            wait = 2.1 - (time.time() - _last_call[0])      # limit 30/min
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        try:
            res = get_json(url, p, timeout=40)
            return res if isinstance(res, list) else []
        except Exception as e:
            err = e
            continue
    raise ConnectionError(f"BZP API niedostępne: {err}")


def fetch_by_notice_number(number: str) -> list[dict]:
    out = []
    for t in TYPES:
        try:
            out += query({"NoticeType": t, "NoticeNumber": number}, page_size=10)
        except ConnectionError:
            continue
        if out:
            break
    return out


def fetch_tender(tender_id: str, org_name: str) -> list[dict]:
    """Wszystkie ogłoszenia postępowania (szukanie po zamawiającym i filtr po tenderId)."""
    out = []
    for t in TYPES:
        try:
            items = query({"NoticeType": t, "OrganizationName": org_name[:60]}, page_size=500)
        except ConnectionError:
            continue
        out += [x for x in items if x.get("tenderId") == tender_id]
    return out


def fetch_org(org_name: str, types=TYPES, page_size: int = 300) -> list[dict]:
    out = []
    for t in types:
        try:
            out += query({"NoticeType": t, "OrganizationName": org_name[:60]}, page_size=page_size)
        except ConnectionError:
            continue
    return out


def ingest_live(conn: sqlite3.Connection, records: list[dict], note: str) -> set[str]:
    started = now_iso()
    touched = ingest_records(conn, records, progress_every=0)
    if touched:
        aggregate_tenders(conn, touched, progress_every=0)
    log_source(conn, "BZP", f"odczyt API: {note}", _bases()[0] + "/mo-board/api/v1/notice", 200, len(records), started_at=started)
    conn.commit()
    return touched


def monitor_tenders(conn: sqlite3.Connection, tender_ids: list[str]) -> list[dict]:
    """Moduł monitorowania: sprawdza nowe ogłoszenia (zmiany/wykonanie) dla trwających postępowań."""
    events = []
    for tid in tender_ids:
        p = conn.execute("SELECT org_name, status FROM procedures WHERE tender_id=?", (tid,)).fetchone()
        if not p:
            continue
        known = {r[0] for r in conn.execute("SELECT notice_number FROM notices WHERE tender_id=?", (tid,))}
        try:
            recs = fetch_tender(tid, p["org_name"] or "")
        except Exception:
            continue
        new = [r for r in recs if r.get("noticeNumber") and r["noticeNumber"] not in known]
        if new:
            ingest_live(conn, new, f"monitoring {tid}")
            for r in new:
                kind = {"AgreementUpdateNotice": "zmiana umowy", "ContractPerformingNotice": "wykonanie umowy",
                        "TenderResultNotice": "wynik postępowania"}.get(r.get("noticeType"), r.get("noticeType"))
                events.append({"tender_id": tid, "notice_number": r["noticeNumber"], "kind": kind, "title": r.get("orderObject")})
    return events
