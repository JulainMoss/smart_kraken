"""Wczytanie zasobów pobranych wcześniej przez harvestery do katalogu data_lake/ (z manifestem SHA-256):
  * bzp_multisector/*.jsonl – ogłoszenia BZP z innych sektorów (drogi, kubatura, energetyka, kolej) -> wielosektorowość,
  * kio/*.jsonl            – orzeczenia KIO (SAOS) -> dokumenty + zdarzenia R11 powiązane z zamawiającymi,
  * ted/*.jsonl            – ogłoszenia TED (PL, wod-kan) -> postępowania źródła TED (identyfikator publikacji),
  * kposk/*                – rejestrowane jako źródła (sprawozdania KPOŚK z dane.gov.pl)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ..build import aggregate_tenders, ingest_records, iter_jsonl
from ..classify import Classifier
from ..db import j, log_source
from ..textutil import fold, now_iso, stem_string, zpack


def ingest_lake(conn: sqlite3.Connection, lake: Path) -> None:
    if not lake.exists():
        print(f"Brak katalogu {lake}")
        return
    manifest = {}
    mf = lake / "manifest.jsonl"
    if mf.exists():
        for ln in mf.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
                manifest[r.get("local_path")] = r
            except json.JSONDecodeError:
                pass
    # 1. BZP wielosektorowe
    for f in sorted((lake / "bzp_multisector").glob("*.jsonl")):
        print(f"[BZP wielosektorowe] {f.name}")
        touched = ingest_records(conn, iter_jsonl(f), progress_every=2000)
        aggregate_tenders(conn, touched, progress_every=2000)
        m = manifest.get(f"bzp_multisector/{f.name}", {})
        log_source(conn, "BZP", "ogłoszenia innych sektorów (API e-Zamówień)", m.get("source_url", "https://ezamowienia.gov.pl/mo-board/api/v1/notice"),
                   200, len(touched), m.get("sha256"), str(f), meta={"sectors": "drogi, kubatura, energetyka, kolej"})
    # 2. KIO
    clf = Classifier(conn)
    orgs = [(r[0], r[1], fold(r[1])) for r in conn.execute("SELECT org_key, name FROM organizations WHERE name LIKE 'Gmina %' OR name LIKE 'Miasto %'")]
    kio_n = 0
    for f in sorted((lake / "kio").glob("*.jsonl")):
        print(f"[KIO] {f.name}")
        for rec in iter_jsonl(f):
            jid = rec.get("id")
            text = rec.get("text_content") or ""
            if not jid or not text:
                continue
            doc_id = f"KIO-{jid}"
            cases = ", ".join(c for c in rec.get("case_numbers") or [] if c)
            ft = fold(text[:6000])
            # zamawiający wymieniony w orzeczeniu (nagłówek wyroku)
            org_hit = None
            for key, name, fname in orgs:
                if len(fname) > 10 and fname in ft:
                    org_hit = (key, name)
                    break
            conn.execute("""INSERT OR REPLACE INTO documents (doc_id,project_id,tender_id,source_system,doc_type,title,url,fetched_at,sha256,mime,pages,chars,text_z,meta,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (doc_id, None, None, "KIO", "orzeczenie KIO", f"Orzeczenie KIO {cases} z {rec.get('judgment_date')}", rec.get("source_url"),
                          rec.get("fetched_at") or now_iso(), None, "text/plain", 1, len(text), zpack(j([[1, text]])),
                          j({"case_numbers": cases, "judgment_type": rec.get("judgment_type"), "org_key": org_hit[0] if org_hit else None,
                             "org_name": org_hit[1] if org_hit else None, "query": rec.get("query_matched")}), "analyzed"))
            # zdarzenie R11 (odwołanie do KIO) z sentencją jako dowodem
            sent = re.search(r"(orzeka:?|postanawia:?)(.{20,600}?)(?:\s+\d\.\s|Uzasadnienie|U z a s a d n i e n i e)", text, re.S | re.I)
            quote = (sent.group(0) if sent else text[:500]).strip()
            res = "oddala" if re.search(r"oddala odwo", fold(quote)) else "uwzględnia" if re.search(r"uwzgledni\w* odwo", fold(quote)) else "umarza/inne"
            conn.execute("DELETE FROM risk_events WHERE doc_id=? AND status='pending'", (doc_id,))
            conn.execute("""INSERT INTO risk_events (doc_id,event_type,risk_code,cause,effect_types,quote,source_system,source_ref,source_url,locator,matched_terms,
                confidence,method,status,created_at,effect_desc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (doc_id, "odwołanie do KIO", "R11", f"Odwołanie wykonawcy – Izba {res} odwołanie ({cases})", j(["termin"]), quote[:900], "KIO", cases,
                          rec.get("source_url"), "sentencja orzeczenia", j(["KIO"]), 0.75, "reguła – orzeczenie KIO", "pending", now_iso(),
                          f"zamawiający: {org_hit[1]}" if org_hit else ""))
            kio_n += 1
        log_source(conn, "KIO/SAOS", f"orzeczenia KIO z data_lake ({f.name})", "https://www.saos.org.pl/api/search/judgments", 200, kio_n,
                   manifest.get(f"kio/{f.name}", {}).get("sha256"), str(f))
    conn.commit()
    print(f"[KIO] {kio_n} orzeczeń")
    # 3. TED
    ted_n = 0
    for f in sorted((lake / "ted").glob("*.jsonl")):
        print(f"[TED] {f.name}")
        for rec in iter_jsonl(f):
            pub = rec.get("publication-number")
            if not pub:
                continue
            t = rec.get("notice-title") or {}
            title = (t.get("pol") or t.get("eng") or "") if isinstance(t, dict) else str(t)
            title = title if isinstance(title, str) else " ".join(title)
            buyer = rec.get("buyer-name") or {}
            bname = (buyer.get("pol") or next(iter(buyer.values()), [""])) if isinstance(buyer, dict) else buyer
            bname = bname[0] if isinstance(bname, list) and bname else str(bname or "")
            tid = f"TED-{pub}"
            date = (rec.get("publication-date") or "")[:10]
            sub, extra = clf.classify_subsector(title)
            conn.execute("""INSERT OR REPLACE INTO notices (notice_number,tender_id,notice_type,pub_date,title,object_id,url,source_system,data,text_z,fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (f"TED {pub}", tid, rec.get("notice-type") or "TED", date, title, pub,
                                                     f"https://ted.europa.eu/pl/notice/-/detail/{pub}", "TED", j({"buyer": bname}), None, now_iso()))
            conn.execute("""INSERT OR REPLACE INTO procedures (tender_id,source_system,sector,subsector,subsectors_extra,title,org_name,first_pub_date,last_pub_date,
                year,features,n_notices,has_outcome,status,risk_codes,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (tid, "TED", "wod-kan" if sub else "inne", sub, j(extra), title, bname, date, date, int(date[:4]) if date else None,
                          j(clf.features_of(title)), 1, 0, "ogłoszone (TED)", j([]), now_iso()))
            old = conn.execute("SELECT fts_rowid FROM fts_map WHERE tender_id=?", (tid,)).fetchone()
            if old:
                conn.execute("DELETE FROM procedures_fts WHERE rowid=?", (old[0],))
            cur = conn.execute("INSERT INTO procedures_fts (tender_id,title_s,desc_s,changes_s) VALUES (?,?,?,?)", (tid, stem_string(title), "", ""))
            conn.execute("INSERT OR REPLACE INTO fts_map VALUES (?,?)", (tid, cur.lastrowid))
            ted_n += 1
        log_source(conn, "TED", f"ogłoszenia TED z data_lake ({f.name})", "https://api.ted.europa.eu/v3/notices/search", 200, ted_n,
                   manifest.get(f"ted/{f.name}", {}).get("sha256"), str(f))
    conn.commit()
    print(f"[TED] {ted_n} ogłoszeń")
    # 4. KPOŚK – rejestr źródeł
    for f in sorted((lake / "kposk").glob("*")):
        m = manifest.get(f"kposk/{f.name}", {})
        log_source(conn, "KPOŚK/dane.gov.pl", f"sprawozdanie/zbiór: {(m.get('meta') or {}).get('resource_title') or f.name}", m.get("source_url") or str(f),
                   200, None, m.get("sha256"), str(f), meta=m.get("meta"))
    conn.commit()
    print("Gotowe.")
