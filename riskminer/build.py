"""Budowa bazy przypadków z ogłoszeń BZP (kopia API e-Zamówień w formacie JSONL lub odczyt na żywo z API).

Etapy (każdy można powtórzyć – proces jest idempotentny):
 1. ingest_jsonl / ingest_records – parsowanie ogłoszeń do tabeli notices (+ organizacje),
 2. aggregate_tenders – złożenie postępowań: umowy, zmiany, wskaźniki przebiegu (opóźnienie, koszt),
 3. detekcja zdarzeń ryzyka z dowodem (cytat + numer ogłoszenia + sekcja formularza),
 4. indeks pełnotekstowy (FTS5) z polskim stemmingiem do wyszukiwania podobnych inwestycji.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import catalog
from .bzp import basis_label, parse_notice
from .classify import Classifier, extract_connections, extract_length_km, time_extension_days
from .db import j, log_source, seed_knowledge, unj
from .textutil import fold, now_iso, parse_date, stem_string, zpack

KEEP_TEXT = {"ContractPerformingNotice", "AgreementUpdateNotice", "TenderResultNotice"}


def iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def org_key(org: dict) -> str:
    return org.get("nip") or ("name:" + fold(org.get("name", ""))[:120])


def ingest_records(conn: sqlite3.Connection, records: Iterable[dict], sector_hint: str | None = None,
                   progress_every: int = 5000, source_system: str = "BZP") -> set[str]:
    """Parsuje rekordy API BZP i zapisuje ogłoszenia. Zwraca zbiór tenderId, które się zmieniły."""
    touched: set[str] = set()
    t0 = time.time()
    n = 0
    batch_n, batch_o = [], {}
    for rec in records:
        try:
            pn = parse_notice(rec)
        except Exception as e:  # pojedyncze uszkodzone ogłoszenie nie zatrzymuje procesu
            sys.stderr.write(f"[parse] {rec.get('noticeNumber')}: {e}\n")
            continue
        if not pn.tender_id:
            continue
        data = dict(pn.data)
        data["parts"] = pn.parts
        data["changes"] = pn.changes
        data["org"] = pn.org
        if sector_hint or rec.get("_sector"):
            data["sector_hint"] = sector_hint or rec.get("_sector")
        batch_n.append((pn.notice_number, pn.tender_id, pn.notice_type, pn.pub_date, pn.title, pn.object_id, pn.url, source_system,
                        j(data), zpack(pn.text[:20000]) if pn.notice_type in KEEP_TEXT else None, now_iso()))
        ok = org_key(pn.org)
        prev = batch_o.get(ok)
        if prev is None or (pn.pub_date or "") >= prev[-1]:
            o = pn.org
            batch_o[ok] = (ok, o.get("nip"), o.get("regon"), o.get("name"), o.get("city"), o.get("province"), o.get("street"),
                           o.get("postcode"), o.get("email"), o.get("phone"), o.get("www"), o.get("org_type"), o.get("nuts"), pn.pub_date or "")
        touched.add(pn.tender_id)
        n += 1
        if len(batch_n) >= 1000:
            _flush(conn, batch_n, batch_o)
            batch_n, batch_o = [], {}
        if progress_every and n % progress_every == 0:
            print(f"  ogłoszenia: {n:,} ({time.time() - t0:.0f} s)", flush=True)
    _flush(conn, batch_n, batch_o)
    return touched


def _flush(conn, batch_n, batch_o):
    if batch_n:
        conn.executemany("INSERT OR REPLACE INTO notices (notice_number,tender_id,notice_type,pub_date,title,object_id,url,source_system,data,text_z,fetched_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch_n)
    for row in batch_o.values():
        old = conn.execute("SELECT last_seen FROM organizations WHERE org_key=?", (row[0],)).fetchone()
        if old and (old[0] or "") > row[-1]:
            continue
        # nie nadpisujemy niepustych danych kontaktowych pustymi
        conn.execute("""INSERT INTO organizations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(org_key) DO UPDATE SET nip=excluded.nip, regon=COALESCE(NULLIF(excluded.regon,''),regon), name=excluded.name,
            city=excluded.city, province=excluded.province, street=excluded.street, postcode=excluded.postcode,
            email=COALESCE(NULLIF(excluded.email,''),email), phone=COALESCE(NULLIF(excluded.phone,''),phone),
            www=COALESCE(NULLIF(excluded.www,''),www), org_type=excluded.org_type, nuts=excluded.nuts, last_seen=excluded.last_seen""", row)
    conn.commit()


# ------------------------------------------------------------------------------------------------
def _sim_name(a: str, b: str) -> float:
    sa = set(re.findall(r"\w{3,}", fold(a)))
    sb = set(re.findall(r"\w{3,}", fold(b)))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _match_contract(contracts: list[dict], value: float | None, contractor: str) -> dict | None:
    best, bs = None, 0.0
    for c in contracts:
        s = 0.0
        cv = c.get("contract_value")
        if value and cv and abs(value - cv) <= max(2.0, 0.005 * cv):
            s += 1.0
        s += _sim_name(contractor or "", c.get("contractor") or "") * 0.8
        if s > bs:
            best, bs = c, s
    return best if bs >= 0.5 else None


def sector_of(subsector: str, cpv_main: str, hint: str | None) -> str:
    if hint:
        return {"drogi_i_mosty": "drogi", "budownictwo_kubaturowe": "kubatura", "energetyka_oze": "energetyka",
                "kolej_transport": "kolej", "wod_kan": "wod-kan"}.get(hint, hint)
    if subsector:
        return "wod-kan"
    code = re.sub(r"\D", "", cpv_main or "")[:8]
    for s in catalog.SECTORS:
        if s["code"] == "wod-kan":
            continue
        if any(code.startswith(p) for p in s["cpv_prefixes"]):
            return s["code"]
    return "inne"


def aggregate_tenders(conn: sqlite3.Connection, tender_ids: Iterable[str] | None = None, clf: Classifier | None = None,
                      progress_every: int = 5000) -> int:
    clf = clf or Classifier(conn)
    if tender_ids is None:
        tender_ids = [r[0] for r in conn.execute("SELECT DISTINCT tender_id FROM notices")]
    tender_ids = list(tender_ids)
    t0 = time.time()
    for i, tid in enumerate(tender_ids, 1):
        _aggregate_one(conn, tid, clf)
        if i % 500 == 0:
            conn.commit()
        if progress_every and i % progress_every == 0:
            print(f"  postępowania: {i:,}/{len(tender_ids):,} ({time.time() - t0:.0f} s)", flush=True)
    conn.commit()
    return len(tender_ids)


def _aggregate_one(conn: sqlite3.Connection, tid: str, clf: Classifier) -> None:
    notices = [dict(r) for r in conn.execute(
        "SELECT notice_number, notice_type, pub_date, title, url, data, source_system FROM notices WHERE tender_id=? ORDER BY pub_date", (tid,))]
    if not notices:
        return
    for n in notices:
        n["d"] = unj(n["data"], {})
    by = {t: [n for n in notices if n["notice_type"] == t] for t in
          ("ContractNotice", "TenderResultNotice", "ContractPerformingNotice", "AgreementUpdateNotice")}
    first = notices[0]
    cn = by["ContractNotice"][0] if by["ContractNotice"] else None
    title = (cn or {}).get("title") or next((n["title"] for n in notices if n["title"]), "")
    desc = ""
    for src in (by["ContractNotice"], by["TenderResultNotice"], by["ContractPerformingNotice"], by["AgreementUpdateNotice"]):
        for n in src:
            if n["d"].get("description"):
                desc = n["d"]["description"]
                break
        if desc:
            break
    org = next((n["d"].get("org") for n in reversed(notices) if n["d"].get("org")), {}) or {}
    if not org and notices[0]["d"].get("buyer"):          # ogłoszenia TED
        org = {"name": notices[0]["d"]["buyer"]}
    est = None
    for n in by["ContractNotice"] + by["TenderResultNotice"]:
        if n["d"].get("estimated_value_net"):
            est = n["d"]["estimated_value_net"]
            break
    cpv_all = first["d"].get("cpv", "")
    cpv_main = (cn or {}).get("d", {}).get("cpv_main") or next((n["d"].get("cpv_main") for n in notices if n["d"].get("cpv_main")), "") or cpv_all.split(",")[0]
    hint = next((n["d"].get("sector_hint") for n in notices if n["d"].get("sector_hint")), None)

    # --- umowy
    contracts: list[dict] = []
    offers = []
    cancelled_parts = 0
    parts_total = 0
    cancel_events = []
    for rn in by["TenderResultNotice"]:
        for p in rn["d"].get("parts", []):
            parts_total += 1
            if p.get("offers") is not None:
                offers.append(p["offers"])
            if p.get("cancelled"):
                cancelled_parts += 1
                cancel_events.append((rn, p))
                continue
            if not (p.get("contractor") or p.get("contract_value")):
                continue
            cid = f"{tid}#{p.get('part') or 1}#{len(contracts) + 1}"
            contracts.append({
                "contract_id": cid, "tender_id": tid, "part": p.get("part") or 1, "contractor": p.get("contractor"),
                "contractor_nip": p.get("contractor_nip"), "contract_date": p.get("contract_date"), "period_txt": p.get("period_txt"),
                "planned_days": p.get("planned_days"), "contract_value": p.get("contract_value"), "result_notice": rn["notice_number"],
                "perf_notice": None, "offers": p.get("offers"), "min_price": p.get("min_price"), "max_price": p.get("max_price"),
                "estimated_value_net": rn["d"].get("estimated_value_net"), "cancelled": 0, "cancel_reason": None,
                "changes": [],
            })
    for pn in by["ContractPerformingNotice"]:
        d = pn["d"]
        c = _match_contract(contracts, d.get("contract_value"), d.get("contractor") or "")
        if c is None:
            c = {"contract_id": f"{tid}#{pn['notice_number']}", "tender_id": tid, "part": None, "contractor": d.get("contractor"),
                 "contractor_nip": d.get("contractor_nip"), "contract_date": d.get("contract_date"), "period_txt": d.get("period_txt"),
                 "planned_days": d.get("planned_days"), "contract_value": d.get("contract_value"), "result_notice": d.get("result_notice"),
                 "perf_notice": None, "offers": None, "min_price": None, "max_price": None, "estimated_value_net": None,
                 "cancelled": 0, "cancel_reason": None, "changes": []}
            contracts.append(c)
        c["perf_notice"] = pn["notice_number"]
        c["perf_url"] = pn["url"]
        for k in ("contract_date", "period_txt", "planned_days", "contract_value", "contractor_nip"):
            if d.get(k):
                c[k] = d[k]
        c["executed"] = d.get("executed")
        c["completion_date"] = d.get("completion_date")
        c["on_time"] = d.get("on_time")
        c["properly"] = d.get("properly")
        c["changes_count"] = d.get("changes_count")
        c["paid_value"] = d.get("paid_value")
        c["additional_info"] = d.get("additional_info") or d.get("not_executed_reason")
        for ch in d.get("changes", []):
            c["changes"].append({**ch, "source_notice": pn["notice_number"], "source_url": pn["url"]})
    for un in by["AgreementUpdateNotice"]:
        d = un["d"]
        c = _match_contract(contracts, d.get("contract_value"), d.get("contractor") or "")
        if c is None:
            c = {"contract_id": f"{tid}#{un['notice_number']}", "tender_id": tid, "part": None, "contractor": d.get("contractor"),
                 "contractor_nip": d.get("contractor_nip"), "contract_date": d.get("contract_date"), "period_txt": d.get("period_txt"),
                 "planned_days": d.get("planned_days"), "contract_value": d.get("contract_value"), "result_notice": d.get("result_notice"),
                 "perf_notice": None, "offers": None, "min_price": None, "max_price": None, "estimated_value_net": None,
                 "cancelled": 0, "cancel_reason": None, "changes": []}
            contracts.append(c)
        existing = {fold(x.get("reason") or "")[:50] for x in c["changes"]}
        for ch in d.get("changes", []):
            key = fold(ch.get("reason") or "")[:50]
            if key and key in existing and c.get("perf_notice"):
                # ta sama zmiana jest już w ogłoszeniu o wykonaniu – zachowujemy dokładniejsze źródło (ogłoszenie o zmianie)
                for x in c["changes"]:
                    if fold(x.get("reason") or "")[:50] == key:
                        x["also_in"] = un["notice_number"]
                continue
            c["changes"].append({**ch, "source_notice": un["notice_number"], "source_url": un["url"]})

    # --- wskaźniki umów
    for c in contracts:
        cd, comp = parse_date(c.get("contract_date")), parse_date(c.get("completion_date"))
        c["actual_days"] = (comp - cd).days if cd and comp and comp >= cd else None
        c["delay_days"] = None
        if c.get("on_time") is False:
            if c["actual_days"] is not None and c.get("planned_days"):
                c["delay_days"] = max(0, c["actual_days"] - c["planned_days"])
        elif c.get("on_time") is True:
            c["delay_days"] = 0
        v, p = c.get("contract_value"), c.get("paid_value")
        c["cost_change_pct"] = round((p - v) / v * 100.0, 2) if (v and v > 1000 and p and p > 0 and c.get("executed") is not False) else None

    # --- agregaty postępowania
    perf = [c for c in contracts if c.get("perf_notice")]
    has_outcome = 1 if perf else 0
    cv_sum = sum(c["contract_value"] for c in perf if c.get("contract_value")) or None
    pv_sum = sum(c["paid_value"] for c in perf if c.get("paid_value")) or None
    ccp = None
    vals = [(c["contract_value"], c["paid_value"]) for c in perf if c.get("cost_change_pct") is not None]
    if vals:
        a, b = sum(x for x, _ in vals), sum(y for _, y in vals)
        ccp = round((b - a) / a * 100.0, 2) if a else None
    delays = [c["delay_days"] for c in perf if c.get("delay_days") is not None]
    actual = [c["actual_days"] for c in perf if c.get("actual_days") is not None]
    planned = next((c["planned_days"] for c in contracts if c.get("planned_days")), None) or (cn or {}).get("d", {}).get("planned_days")
    delayed = 1 if any(c.get("on_time") is False for c in perf) else (0 if perf else None)
    improper = 1 if any(c.get("properly") is False for c in perf) else (0 if perf else None)
    not_exec = 1 if any(c.get("executed") is False for c in perf) else (0 if perf else None)
    all_changes = [ch for c in contracts for ch in c["changes"]]
    if has_outcome:
        status = "zakończone"
    elif contracts:
        status = "w realizacji"
    elif parts_total and cancelled_parts == parts_total:
        status = "unieważnione"
    else:
        status = "ogłoszone"

    text_all = f"{title}\n{desc}"
    sub, extra = clf.classify_subsector(title, desc)
    sector = sector_of(sub, cpv_main, hint)
    feats = clf.features_of(text_all)
    cnd = (cn or {}).get("d", {})
    price_w = None
    for cr in cnd.get("criteria", []) or []:
        if cr.get("kryterium") and fold(cr["kryterium"]).startswith("cena") and cr.get("waga"):
            price_w = cr["waga"]

    # --- zapis
    conn.execute("DELETE FROM contracts WHERE tender_id=?", (tid,))
    conn.execute("DELETE FROM changes WHERE tender_id=?", (tid,))
    conn.execute("DELETE FROM risk_events INDEXED BY ix_event_tender WHERE tender_id=? AND method LIKE 'reguła%' AND status='pending'", (tid,))
    reviewed_keys = {(r[0], r[1]) for r in conn.execute(
        "SELECT source_ref, risk_code FROM risk_events INDEXED BY ix_event_tender WHERE tender_id=? AND status!='pending'", (tid,))}
    codes_found: dict[str, int] = {}
    ts = now_iso()

    def add_event(**ev):
        key = (ev.get("source_ref"), ev.get("risk_code"))
        if key in reviewed_keys:   # decyzja eksperta ma pierwszeństwo – nie dublujemy
            return
        codes_found[ev["risk_code"]] = codes_found.get(ev["risk_code"], 0) + 1
        cols = ["tender_id", "change_id", "event_type", "risk_code", "cause", "effect_types", "effect_time_days", "effect_cost_pln", "effect_desc",
                "actions", "action_effectiveness", "quote", "source_system", "source_ref", "source_url", "locator", "matched_terms",
                "confidence", "method", "status", "created_at"]
        ev.setdefault("tender_id", tid)
        ev.setdefault("source_system", "BZP")
        ev.setdefault("method", "reguła (katalog ekspercki)")
        ev.setdefault("status", "pending")
        ev.setdefault("created_at", ts)
        conn.execute(f"INSERT INTO risk_events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [ev.get(k) for k in cols])

    for c in contracts:
        conn.execute("""INSERT OR REPLACE INTO contracts (contract_id,tender_id,part,contractor,contractor_nip,contract_date,period_txt,planned_days,
            contract_value,result_notice,perf_notice,offers,min_price,max_price,estimated_value_net,cancelled,cancel_reason,executed,completion_date,
            on_time,properly,changes_count,paid_value,cost_change_pct,actual_days,delay_days,additional_info) VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (c["contract_id"], tid, c.get("part"), c.get("contractor"), c.get("contractor_nip"), c.get("contract_date"), c.get("period_txt"),
                      c.get("planned_days"), c.get("contract_value"), c.get("result_notice"), c.get("perf_notice"), c.get("offers"), c.get("min_price"),
                      c.get("max_price"), c.get("estimated_value_net"), 0, None, _b(c.get("executed")), c.get("completion_date"), _b(c.get("on_time")),
                      _b(c.get("properly")), c.get("changes_count"), c.get("paid_value"), c.get("cost_change_pct"), c.get("actual_days"),
                      c.get("delay_days"), c.get("additional_info")))
        # skuteczność działań – oceniana po wyniku umowy
        if c.get("perf_notice"):
            if c.get("executed") is False or c.get("properly") is False:
                eff = "nieskuteczne – umowa niewykonana lub wykonana nienależycie"
            elif c.get("on_time") is False:
                eff = "częściowo skuteczne – umowę wykonano, ale po pierwotnym terminie"
            else:
                eff = "skuteczne – umowę wykonano w terminie i należycie"
        else:
            eff = "nieznana – brak ogłoszenia o wykonaniu umowy"
        explained_delay = False
        for ch in c["changes"]:
            txt = f"{ch.get('reason') or ''}. {ch.get('description') or ''}"
            effects = clf.effect_types(txt)
            acts = clf.actions(txt)
            text_ext = time_extension_days(txt)
            cur = conn.execute("""INSERT INTO changes (tender_id,contract_id,source_notice,source_type,section,change_no,change_date,legal_basis,basis_label,
                reason,description,value,price_increase,effect_types,time_ext_days,actions) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (tid, c["contract_id"], ch.get("source_notice"), ch.get("source_type"), ch.get("reason_section") or ch.get("section"),
                                ch.get("change_no"), ch.get("change_date"), ch.get("legal_basis"), basis_label(ch.get("legal_basis")),
                                ch.get("reason"), ch.get("description"), ch.get("value"), _b(ch.get("price_increase")), j(effects), text_ext, j(acts)))
            change_id = cur.lastrowid
            codes = [x for x in clf.classify_risk(txt) if x["confidence"] >= 0.45]
            if clf.is_administrative(txt, codes):
                continue   # zmiana administracyjna – zapisana w tabeli changes, bez zdarzenia ryzyka
            et = "zmiana terminu" if "termin" in effects else "zmiana wynagrodzenia" if (ch.get("price_increase") or "koszt" in effects) else "zmiana zakresu/umowy"
            if "termin" in effects:
                explained_delay = True
            quote = (ch.get("reason") or "").strip()
            if ch.get("description"):
                quote = (quote + " — " + ch["description"].strip()) if quote else ch["description"].strip()
            locator = f"sekcja {ch.get('reason_section') or ch.get('section') or ''} ({'ogłoszenie o zmianie umowy' if ch.get('source_type') == 'AgreementUpdateNotice' else 'ogłoszenie o wykonaniu umowy, 5.4'})"
            common = dict(change_id=change_id, event_type=et, cause=ch.get("reason"), effect_types=j(effects),
                          effect_time_days=text_ext or (c.get("delay_days") if "termin" in effects else None),
                          effect_cost_pln=ch.get("value") if ch.get("price_increase") else None,
                          effect_desc=(ch.get("description") or "")[:600], actions=j(acts), action_effectiveness=eff,
                          quote=quote[:900], source_ref=ch.get("source_notice"), source_url=ch.get("source_url"), locator=locator)
            if codes:
                for x in codes:
                    add_event(risk_code=x["code"], matched_terms=j(x["terms"]), confidence=x["confidence"], **common)
            elif quote:
                add_event(risk_code="BRAK", matched_terms=j([]), confidence=0.2,
                          **{**common, "method": "reguła – brak dopasowania (do klasyfikacji przez eksperta)"})
        if c.get("perf_notice"):
            if c.get("on_time") is False and not explained_delay:
                add_event(event_type="wykonanie po terminie", risk_code="R06", cause="Umowę wykonano po pierwotnie określonym terminie (przyczyna nieopisana w ogłoszeniu).",
                          effect_types=j(["termin"]), effect_time_days=c.get("delay_days"), quote="5.3.) Czy umowę wykonano w pierwotnie określonym terminie: Nie",
                          source_ref=c["perf_notice"], source_url=c.get("perf_url"), locator="sekcja 5.3 ogłoszenia o wykonaniu umowy",
                          matched_terms=j(["5.3: Nie"]), confidence=0.55, action_effectiveness=eff)
            if c.get("executed") is False:
                q = c.get("additional_info") or "5.1.) Czy umowa została wykonana: Nie"
                codes = clf.classify_risk(q) or [{"code": "R12", "confidence": 0.6, "terms": ["5.1: Nie"]}]
                add_event(event_type="niewykonanie umowy", risk_code=codes[0]["code"], cause=q[:500], effect_types=j(["termin", "zakres"]),
                          quote=q[:900], source_ref=c["perf_notice"], source_url=c.get("perf_url"), locator="sekcja 5.1/VI ogłoszenia o wykonaniu umowy",
                          matched_terms=j(codes[0].get("terms")), confidence=max(0.6, codes[0]["confidence"]), action_effectiveness=eff)
            elif c.get("properly") is False:
                add_event(event_type="nienależyte wykonanie", risk_code="R10", cause="Umowę wykonano nienależycie (5.6: Nie).", effect_types=j(["jakość"]),
                          quote="5.6.) Czy umowa została wykonana należycie: Nie" + (f" — {c['additional_info']}" if c.get("additional_info") else ""),
                          source_ref=c["perf_notice"], source_url=c.get("perf_url"), locator="sekcja 5.6 ogłoszenia o wykonaniu umowy",
                          matched_terms=j(["5.6: Nie"]), confidence=0.6, action_effectiveness=eff)
            if c.get("cost_change_pct") is not None and c["cost_change_pct"] > 2 and not any(ch.get("price_increase") for ch in c["changes"]):
                add_event(event_type="wypłata wyższa niż wartość umowy", risk_code="R07",
                          cause=f"Wypłacono {c['cost_change_pct']:+.1f}% względem wartości umowy (przyczyna nieopisana).", effect_types=j(["koszt"]),
                          effect_cost_pln=(c.get("paid_value") or 0) - (c.get("contract_value") or 0),
                          quote=f"4.4.) Wartość umowy: {c.get('contract_value'):.2f} PLN; 5.5.) Łączna wartość wypłacona: {c.get('paid_value'):.2f} PLN",
                          source_ref=c["perf_notice"], source_url=c.get("perf_url"), locator="sekcje 4.4 i 5.5 ogłoszenia o wykonaniu umowy",
                          matched_terms=j(["5.5 > 4.4"]), confidence=0.5, action_effectiveness=eff)
    for rn, p in cancel_events:
        q = p.get("cancel_reason") or p.get("result") or "Postępowanie unieważniono"
        basis = p.get("cancel_basis") or ""
        txt = f"{basis} {q}"
        codes = clf.classify_risk(txt)
        code = "R13" if re.search(r"255\s*pkt\.?\s*3", basis) else (codes[0]["code"] if codes else "R11")
        add_event(event_type="unieważnienie postępowania", risk_code=code, cause=(basis + ": " + q)[:600] if basis else q[:600],
                  effect_types=j(["termin"]), quote=(q or "")[:900], source_ref=rn["notice_number"], source_url=rn["url"],
                  locator=f"sekcja V ogłoszenia o wyniku postępowania (część {p.get('part') or 1})",
                  matched_terms=j(([basis] if basis else []) + (codes[0]["terms"] if codes else [])), confidence=0.8 if basis else 0.55,
                  action_effectiveness="postępowanie wymagało powtórzenia")

    changes_text = " ".join(f"{ch.get('reason') or ''} {ch.get('description') or ''}" for ch in all_changes)
    conn.execute("""INSERT OR REPLACE INTO procedures (tender_id,source_system,sector,subsector,subsectors_extra,works_type,design_build,title,description,
        org_key,org_name,nip,province,city,cpv_main,cpv_all,estimated_value_net,eu_funded,tryb,platform_url,planned_days,period_txt,changes_foreseen,
        price_weight,first_pub_date,last_pub_date,year,length_km,connections,features,n_notices,n_contracts,n_changes,offers_avg,cancelled_parts,parts,
        contract_value,paid_value,cost_change_pct,actual_days,delay_days,delayed,improper,not_executed,has_outcome,status,risk_codes,exemplary,expert_note,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
        COALESCE((SELECT exemplary FROM procedures WHERE tender_id=?),0),(SELECT expert_note FROM procedures WHERE tender_id=?),?)""",
                 (tid, notices[0].get("source_system") or "BZP", sector, sub, j(extra), clf.works_type(title or desc), 1 if clf.design_build(f"{title} {desc[:1500]}") else 0,
                  title, desc, org_key(org), org.get("name"), org.get("nip"), org.get("province"), org.get("city"), cpv_main, cpv_all, est,
                  _b(cnd.get("eu_funded") if cn else next((n["d"].get("eu_funded") for n in notices if n["d"].get("eu_funded") is not None), None)),
                  cnd.get("tryb"), cnd.get("platform_url"), planned, cnd.get("period_txt") or next((c.get("period_txt") for c in contracts if c.get("period_txt")), None),
                  _b(cnd.get("changes_foreseen")), price_w, notices[0]["pub_date"], notices[-1]["pub_date"],
                  int(notices[0]["pub_date"][:4]) if notices[0]["pub_date"] else None, extract_length_km(text_all), extract_connections(text_all),
                  j(feats), len(notices), len(contracts), len(all_changes), round(sum(offers) / len(offers), 2) if offers else None,
                  cancelled_parts, parts_total, cv_sum, pv_sum, ccp, max(actual) if actual else None, max(delays) if delays else None,
                  delayed, improper, not_exec, has_outcome, status, j(sorted(codes_found)), tid, tid, now_iso()))
    old = conn.execute("SELECT fts_rowid FROM fts_map WHERE tender_id=?", (tid,)).fetchone()
    if old:
        conn.execute("DELETE FROM procedures_fts WHERE rowid=?", (old[0],))
    cur = conn.execute("INSERT INTO procedures_fts (tender_id,title_s,desc_s,changes_s) VALUES (?,?,?,?)",
                       (tid, stem_string(title), stem_string(desc[:4000]), stem_string(changes_text[:4000])))
    conn.execute("INSERT OR REPLACE INTO fts_map VALUES (?,?)", (tid, cur.lastrowid))


def _b(v) -> int | None:
    return None if v is None else (1 if v else 0)


def build_from_mirror(conn: sqlite3.Connection, jsonl: Path, limit: int | None = None, sector_hint: str | None = None) -> None:
    seed_knowledge(conn)
    started = now_iso()
    print(f"[1/2] Wczytuję ogłoszenia z {jsonl} ...", flush=True)

    def gen():
        for i, r in enumerate(iter_jsonl(jsonl)):
            if limit and i >= limit:
                break
            yield r
    touched = ingest_records(conn, gen(), sector_hint=sector_hint)
    print(f"[2/2] Składam {len(touched):,} postępowań (umowy, zmiany, zdarzenia ryzyka, indeks) ...", flush=True)
    aggregate_tenders(conn, sorted(touched))
    n = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
    log_source(conn, "BZP", "import kopii ogłoszeń (JSONL)", str(jsonl), 200, n, started_at=started,
               meta={"limit": limit, "tenders": len(touched)})
    conn.commit()
