"""Import danych (pliki) i automatyczne wzbogacanie projektów o dane ze źródeł publicznych.

* import_table – rekordy projektów z .xlsx/.xls/.csv (format MFiPR lub dowolny z rozpoznawaniem kolumn),
* validate_project – normalizacja i weryfikacja identyfikatorów (NIP – suma kontrolna, numer BZP, numer KPO vs województwo),
* link_project – łączenie z postępowaniami: 1) numer ogłoszenia BZP (pewne), 2) NIP + podobieństwo nazwy/wartości/czasu
  (z poziomem pewności i uzasadnieniem; niepewne trafiają do eksperta),
* enrich_project – BZP (lokalna kopia + API), TED, KIO (SAOS), BIP zamawiającego, NIK; zdarzenia ryzyka z dokumentów,
* import_document – wgranie dokumentu (PDF/DOCX/...) do projektu i analiza fragmentów pod kątem ryzyk.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .classify import time_extension_days, Classifier
from .db import j, log_expert, log_source, one, rows, unj
from .docs import full_text, read_any, split_sentences
from .query import PROVINCES, TERYT_TO_PL
from .textutil import bzp_base, digits, fold, nip_valid, norm_bzp_number, now_iso, parse_amount, stem, tokens, zpack

COLS = {
    "project_id": [r"identyfikator rekordu", r"project_id", r"^id$", r"id projektu", r"identyfikator projektu"],
    "org_id_type": [r"rodzaj identyfikatora odbiorcy"],
    "org_id": [r"^identyfikator odbiorcy", r"^nip", r"nip odbiorcy", r"nip zamawiaj", r"nip beneficjenta"],
    "org_name": [r"nazwa odbiorcy", r"zamawiaj", r"beneficjent", r"nazwa podmiotu", r"inwestor"],
    "org_type": [r"typ odbiorcy", r"typ beneficjenta"],
    "teryt": [r"kod wojew", r"teryt"],
    "province": [r"^wojew"],
    "city": [r"siedziba", r"miejscowo", r"lokalizac"],
    "title": [r"nazwa przedsi", r"tytu", r"nazwa projektu", r"nazwa zadania", r"przedmiot", r"nazwa inwestycji"],
    "scope": [r"zakres rekordu", r"^zakres"],
    "public_id_type": [r"rodzaj identyfikatora publicznego", r"typ identyfikatora"],
    "public_id": [r"^identyfikator publiczny", r"numer ogłoszenia", r"numer projektu", r"nr ogłoszenia", r"ocid", r"tenderid"],
    "theme": [r"obszar tematyczny", r"sektor", r"dziedzina"],
    "order_type": [r"rodzaj zamówienia"],
    "legal_regime": [r"reżim prawny", r"tryb"],
    "currency": [r"walut"],
    "status": [r"status"],
    "record_source": [r"źródło rekordu"],
    "record_date": [r"data sporządzenia", r"data rekordu"],
    "record_version": [r"wersja rekordu"],
    "annotation": [r"adnotacja", r"opis", r"uwagi"],
    "value": [r"wartość", r"kwota", r"koszt"],
}

KPO_RE = re.compile(r"KPO[A-Z]?\.?\d{2}\.\d{2}-IW\.(\d{2})-\d{4}/\d{2}", re.I)
# kolejność województw wg TERYT (02..32) – w numerach naborów KPO B3.1.1: IW.13 = dolnośląskie ... IW.28 = zachodniopomorskie
KPO_REGION = {13 + i: f"PL{2 * (i + 1):02d}" for i in range(16)}


def _map_columns(header: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, pats in COLS.items():
        for i, h in enumerate(header):
            hl = (h or "").strip().lower()
            if any(re.search(p, hl) for p in pats) and i not in out.values():
                out[key] = i
                break
    return out


def parse_annotation(a: str | None) -> dict[str, Any]:
    a = a or ""
    out: dict[str, Any] = {}
    m = re.search(r"Wartość przedsięwzięcia:\s*([\d\s ]+,\d{2})", a)
    if m:
        out["project_value"] = parse_amount(m.group(1))
    m = re.search(r"dofinansowanie:\s*([\d\s ]+,\d{2})", a)
    if m:
        out["grant_value"] = parse_amount(m.group(1))
    m = re.search(r"Opis:\s*[„\"](.+?)[”\"]\s*$", a, re.S)
    if m:
        out["description"] = m.group(1).strip()
    prog = []
    for pat, name in ((r"\bKPO\b", "KPO"), (r"PROW|EFRROW|Obszarów Wiejskich", "PROW 2014–2020"), (r"FEnIKS|FENX", "FEnIKS"),
                      (r"RPO|Regionaln", "RPO"), (r"Polski Ład|Rządowy Fundusz", "RFIL / Polski Ład")):
        if re.search(pat, a, re.I):
            prog.append(name)
    out["programme"] = ", ".join(prog)
    return out


def import_table(conn: sqlite3.Connection, filename: str, data: bytes, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    parsed = read_any(filename, data)
    rows_ = parsed.get("rows") or []
    if not rows_:
        raise ValueError("Plik nie zawiera tabeli z danymi.")
    # wiersz nagłówka: pierwszy, który ma >=3 rozpoznane kolumny
    hdr_i, cmap = 0, {}
    for i, r in enumerate(rows_[:15]):
        m = _map_columns([str(x) for x in r])
        if len(m) >= 3:
            hdr_i, cmap = i, m
            break
    if "title" not in cmap and "public_id" not in cmap:
        raise ValueError("Nie rozpoznano kolumn (oczekiwane m.in.: nazwa przedsięwzięcia, identyfikator publiczny, NIP).")
    header = [str(x) for x in rows_[hdr_i]]
    imported = []
    ts = now_iso()
    clf = Classifier(conn)
    for r in rows_[hdr_i + 1:]:
        if not any(str(x).strip() for x in r):
            continue
        g = lambda k: (str(r[cmap[k]]).strip() if k in cmap and cmap[k] < len(r) and r[cmap[k]] is not None else "")
        title = g("title")
        if not title:
            continue
        pid = g("project_id") or ("RM-IMP-" + hashlib.sha1((title + g("org_id")).encode()).hexdigest()[:8].upper())
        ann = parse_annotation(g("annotation"))
        teryt = g("teryt").split(".")[0].zfill(2) if g("teryt") else ""
        prov_code = TERYT_TO_PL.get(teryt) or next((c for c, (n, _) in PROVINCES.items() if n == g("province").lower()), "")
        raw = {header[i] if i < len(header) else f"kol{i}": (str(v) if v is not None else "") for i, v in enumerate(r)}
        rec = {
            "project_id": pid, "title": title, "org_name": g("org_name"), "org_id_type": g("org_id_type") or "NIP", "org_id": digits(g("org_id")) or g("org_id"),
            "org_type": g("org_type"), "province": prov_code, "teryt": teryt, "city": g("city"), "scope": g("scope"),
            "public_id_type": g("public_id_type"), "public_id": g("public_id"), "public_id_norm": norm_bzp_number(g("public_id")) or g("public_id").strip(),
            "theme": g("theme"), "order_type": g("order_type"), "legal_regime": g("legal_regime"), "currency": g("currency") or "PLN",
            "status": g("status"), "record_source": g("record_source"), "record_date": g("record_date")[:10], "record_version": g("record_version"),
            "annotation": g("annotation"), "project_value": ann.get("project_value") or (parse_amount(g("value")) if g("value") else None),
            "grant_value": ann.get("grant_value"), "programme": ann.get("programme"), "source_file": filename, "raw": j(raw), "imported_at": ts,
        }
        sub, _ = clf.classify_subsector(title, ann.get("description", ""))
        rec["sector"] = "wod-kan" if sub else ""
        rec["subsector"] = sub
        rec["validation"] = j(validate_project(conn, rec))
        cols = list(rec.keys())
        old = one(conn, "SELECT title, org_id FROM projects WHERE project_id=?", (pid,))
        if old and (fold(old["title"] or "") != fold(title) or digits(old["org_id"] or "") != digits(rec["org_id"] or "")):
            # pod tym samym identyfikatorem przyszedł INNY projekt (inna nazwa lub NIP): powiązania, dokumenty i zdarzenia
            # poprzedniego rekordu nie mogą przejść na nowy – odpinamy je (dane źródłowe zostają w bazie)
            conn.execute("DELETE FROM project_links WHERE project_id=?", (pid,))
            conn.execute("UPDATE documents SET project_id=NULL WHERE project_id=?", (pid,))
            conn.execute("UPDATE risk_events SET project_id=NULL WHERE project_id=?", (pid,))
            if progress:
                progress(f"Uwaga: identyfikator {pid} był użyty dla innego projektu („{(old['title'] or '')[:60]}”) – poprzednie powiązania odpięto.")
        conn.execute(f"INSERT INTO projects ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                     f"ON CONFLICT(project_id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'project_id')}",
                     [rec[c] for c in cols])
        imported.append(pid)
        n_rec = record_events(conn, pid)
        project_case(conn, pid)
        if progress:
            progress(f"Wczytano projekt {pid}: {title[:60]}" + (f" – w treści rekordu rozpoznano {n_rec} zdarzeń ryzyka" if n_rec else ""))
    log_source(conn, "PLIK", f"import tabeli projektów ({len(imported)} rekordów)", filename, 200, len(imported),
               sha256=hashlib.sha256(data).hexdigest())
    conn.commit()
    return {"imported": imported, "columns": {k: header[v] for k, v in cmap.items()}, "header_row": hdr_i + 1}


def validate_project(conn: sqlite3.Connection, p: dict) -> list[dict[str, Any]]:
    """Weryfikacja identyfikatorów i spójności danych (błędy/braki w danych wejściowych są częścią wyzwania)."""
    out = []

    def add(level, field, msg):
        out.append({"level": level, "field": field, "message": msg})

    nip = digits(p.get("org_id"))
    if (p.get("org_id_type") or "NIP").upper().startswith("NIP"):
        if not nip:
            add("error", "NIP", "Brak NIP odbiorcy.")
        elif not nip_valid(nip):
            add("error", "NIP", f"NIP {nip} ma niepoprawną sumę kontrolną.")
        else:
            o = one(conn, "SELECT name FROM organizations WHERE nip=? LIMIT 1", (nip,))
            if o and p.get("org_name") and _name_sim(o["name"], p["org_name"]) < 0.3:
                add("warning", "NIP", f"NIP {nip} w BZP należy do „{o['name']}” – nazwa w rekordzie: „{p['org_name']}”.")
            elif o:
                add("ok", "NIP", f"NIP poprawny i zgodny z BZP („{o['name']}”).")
            else:
                add("ok", "NIP", "NIP poprawny (suma kontrolna).")
    pid_type = (p.get("public_id_type") or "").lower()
    pub = p.get("public_id") or ""
    if "bzp" in pid_type or re.search(r"BZP", pub, re.I):
        n = norm_bzp_number(pub)
        if not n:
            add("error", "Identyfikator publiczny", f"Niepoprawny format numeru ogłoszenia BZP: „{pub}”.")
        elif n != pub.strip():
            add("info", "Identyfikator publiczny", f"Numer znormalizowano: „{pub}” → „{n}”.")
    elif "kpo" in pid_type or pub.upper().startswith("KPO"):
        m = KPO_RE.search(pub)
        if not m:
            add("warning", "Identyfikator publiczny", f"Nietypowy format numeru projektu KPO: „{pub}”.")
        else:
            reg = KPO_REGION.get(int(m.group(1)))
            if reg and p.get("province") and reg != p["province"]:
                add("warning", "Identyfikator publiczny", f"Kod regionu w numerze KPO (IW.{m.group(1)}) wskazuje na woj. {PROVINCES[reg][0]}, "
                                                          f"a rekord podaje {PROVINCES.get(p['province'], ('?',))[0]}.")
            else:
                add("ok", "Identyfikator publiczny", "Format numeru KPO poprawny; kod regionu zgodny z województwem.")
    if p.get("teryt") and p.get("province") and TERYT_TO_PL.get(p["teryt"]) != p["province"]:
        add("warning", "Województwo", "Kod TERYT nie zgadza się z nazwą województwa.")
    if not p.get("title"):
        add("error", "Nazwa", "Brak nazwy przedsięwzięcia.")
    return out


def _name_sim(a: str, b: str) -> float:
    sa = {stem(t) for t in tokens(a)}
    sb = {stem(t) for t in tokens(b)}
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def _title_sim(a: str, b: str) -> tuple[float, list[str]]:
    generic = {stem(w) for w in ("budowa", "budowy", "sieci", "sieć", "wraz", "gminy", "gmina", "gminie", "terenie", "miejscowości", "zadania",
               "realizacja", "przebudowa", "rozbudowa", "modernizacja", "sanitarnej", "kanalizacji", "kanalizacyjnej", "wodociągowej",
               "wodociągu", "przyłączami", "ścieków", "oczyszczalni", "budowie", "odcinka", "odcinków", "ulicy", "deszczowej")}
    sa = {stem(t) for t in tokens(a)}
    sb = {stem(t) for t in tokens(b)}
    if not sa or not sb:
        return 0.0, []
    common = sa & sb
    base = len(common) / len(sa | sb)
    # rzadkie słowa (nazwy miejscowości) ważą więcej
    rare = [w for w in common if w not in generic and not w.isdigit() and len(w) >= 4]
    bonus = min(0.4, 0.12 * len(rare))
    return min(1.0, base + bonus), sorted(rare)


def link_project(conn: sqlite3.Connection, project_id: str, live: bool = True, progress: Callable[[str], None] | None = None) -> list[dict]:
    from .sources import bzp_api
    p = one(conn, "SELECT * FROM projects WHERE project_id=?", (project_id,))
    if not p:
        raise KeyError(project_id)
    say = progress or (lambda m: None)
    nip = digits(p.get("org_id"))
    links: list[dict] = []
    pub_norm = norm_bzp_number(p.get("public_id"))
    ann = parse_annotation(p.get("annotation"))
    # 1) dokładny numer ogłoszenia BZP
    if pub_norm:
        say(f"Szukam ogłoszenia {pub_norm} w BZP…")
        r = one(conn, "SELECT tender_id, notice_number FROM notices WHERE notice_number=? OR notice_number LIKE ?", (pub_norm, bzp_base(pub_norm) + "/%"))
        if not r and live:
            try:
                recs = bzp_api.fetch_by_notice_number(bzp_base(pub_norm))
                if recs:
                    say(f"Pobrano {len(recs)} ogłoszeń z API BZP")
                    tid = recs[0].get("tenderId")
                    more = bzp_api.fetch_tender(tid, recs[0].get("organizationName") or p.get("org_name") or "")
                    bzp_api.ingest_live(conn, recs + more, f"numer {pub_norm}")
                    r = one(conn, "SELECT tender_id, notice_number FROM notices WHERE notice_number LIKE ?", (bzp_base(pub_norm) + "/%",))
            except Exception as e:
                say(f"API BZP niedostępne: {e}")
        via_ref = False
        if not r:
            # numer nie występuje jako ogłoszenie, ale może być przywołany w ogłoszeniu o wyniku/zmianie/wykonaniu umowy
            for pat in (f'%"contract_notice": "{pub_norm}"%', f'%"result_notice": "{pub_norm}"%'):
                r = one(conn, "SELECT tender_id, notice_number FROM notices WHERE data LIKE ? LIMIT 1", (pat,))
                if r:
                    via_ref = True
                    say(f"Numer {pub_norm} przywołany w ogłoszeniu {r['notice_number']}")
                    break
        if r:
            proc = one(conn, "SELECT * FROM procedures WHERE tender_id=?", (r["tender_id"],)) or {}
            conf, ev = (0.95, [f"ogłoszenie {pub_norm} nie występuje w BZP/API, ale jest przywołane jako ogłoszenie o zamówieniu w {r['notice_number']}"]) \
                if via_ref else (1.0, [f"numer ogłoszenia {pub_norm} znaleziony w BZP"])
            status = "auto"
            if nip and proc.get("nip") and proc["nip"] != nip:
                conf, status = 0.6, "uncertain"
                ev.append(f"UWAGA: NIP zamawiającego w BZP ({proc['nip']}) różni się od NIP w rekordzie ({nip})")
            elif nip and proc.get("nip") == nip:
                ev.append("NIP zamawiającego zgodny")
            ts_, rare = _title_sim(p["title"], proc.get("title") or "")
            ev.append(f"podobieństwo nazwy {ts_:.0%}" + (f" (wspólne: {', '.join(rare[:5])})" if rare else ""))
            if ts_ < 0.25:
                conf, status = min(conf, 0.7), "uncertain"
                ev.append("UWAGA: nazwa przedsięwzięcia słabo zgodna z nazwą postępowania")
            links.append({"target_id": r["tender_id"], "title": proc.get("title"),
                          "basis": "numer ogłoszenia BZP (odwołanie w innym ogłoszeniu)" if via_ref else "numer ogłoszenia BZP", "confidence": conf,
                          "evidence": ev, "status": status})
    # 2) NIP + podobieństwo (projekty KPO, brak numeru, lub dodatkowe postępowania tego samego projektu)
    cands = rows(conn, "SELECT * FROM procedures WHERE nip=? AND tender_id NOT LIKE 'PROJ-%'", (nip,)) if nip else []
    if live and nip and (not links or not cands or not pub_norm):
        try:
            say(f"Pobieram ogłoszenia zamawiającego „{p['org_name']}” z API BZP…")
            short = re.split(r"\s+[–-]\s+", p.get("org_name") or "")[0]
            recs = [x for x in bzp_api.fetch_org(short) if digits(x.get("organizationNationalId")) in (nip, "")]
            if recs:
                bzp_api.ingest_live(conn, recs, f"zamawiający {short}")
                cands = rows(conn, "SELECT * FROM procedures WHERE nip=? AND tender_id NOT LIKE 'PROJ-%'", (nip,))
        except Exception as e:
            say(f"API BZP niedostępne: {e}")
    linked = {l["target_id"] for l in links}
    scored = []
    kpo_year = None
    m = re.search(r"/(\d{2})$", p.get("public_id") or "")
    if m and (p.get("public_id") or "").upper().startswith("KPO"):
        kpo_year = 2000 + int(m.group(1))
    for c in cands:
        if c["tender_id"] in linked:
            continue
        ts_, rare = _title_sim(p["title"] + " " + ann.get("description", "")[:300], (c.get("title") or "") + " " + (c.get("description") or "")[:300])
        ev = [f"ten sam NIP zamawiającego ({nip})", f"podobieństwo nazwy/opisu {ts_:.0%}" + (f" (wspólne: {', '.join(rare[:6])})" if rare else "")]
        s = 0.35 + 0.55 * ts_
        if kpo_year and c.get("year"):
            if c["year"] >= kpo_year - 1:
                s += 0.05
                ev.append(f"rok postępowania {c['year']} zgodny z naborem KPO ({kpo_year})")
            else:
                s -= 0.15
                ev.append(f"postępowanie z {c['year']} – wcześniejsze niż nabór KPO ({kpo_year})")
        pv = p.get("project_value") or ann.get("project_value")
        cv = c.get("contract_value") or (c.get("estimated_value_net") or 0) * 1.23
        if pv and cv:
            ratio = cv / pv
            if 0.5 <= ratio <= 1.3:
                s += 0.07
                ev.append(f"wartość umowy {cv:,.0f} zł zgodna ze skalą projektu ({pv:,.0f} zł)".replace(",", " "))
        if c.get("subsector") and p.get("subsector") and c["subsector"] == p["subsector"]:
            s += 0.03
        scored.append((min(0.97, s), c, ev, ts_))
    scored.sort(key=lambda x: -x[0])
    second = scored[1][0] if len(scored) > 1 else 0.0
    for i, (s, c, ev, ts_) in enumerate(scored[:4]):
        if links:
            # projekt ma już pewne postępowanie – inne tylko gdy bardzo podobne (np. powtórzone po unieważnieniu)
            if ts_ < 0.6 or s < 0.75:
                continue
        elif ts_ < 0.25 or s < 0.55:
            continue
        status = "auto" if (i == 0 and s >= 0.8 and s - second >= 0.08 and not links) else "uncertain"
        basis = "NIP + podobieństwo nazwy" if not links else "powiązane postępowanie (NIP + nazwa)"
        links.append({"target_id": c["tender_id"], "title": c.get("title"), "basis": basis, "confidence": round(s, 2), "evidence": ev, "status": status})
    # wnioski z łączenia trafiają do walidacji rekordu (błędy/braki w danych wejściowych)
    val = [v for v in (unj(p.get("validation"), []) or []) if v.get("field") != "Łączenie z BZP"]
    if pub_norm and not any(l["basis"] == "numer ogłoszenia BZP" for l in links):
        if any("odwołanie" in l["basis"] for l in links):
            val.append({"level": "warning", "field": "Łączenie z BZP", "message": f"Ogłoszenie {pub_norm} nie występuje w BZP ani w API; postępowanie odnaleziono "
                        f"przez odwołanie do tego numeru w innym ogłoszeniu – sprawdź numer w rekordzie."})
        else:
            val.append({"level": "error", "field": "Łączenie z BZP", "message": f"Ogłoszenie {pub_norm} nie występuje w BZP ani w API."})
    if not any(l["status"] == "auto" for l in links):
        val.append({"level": "warning", "field": "Łączenie z BZP", "message": "Brak pewnego dopasowania postępowania – powiązania wymagają decyzji eksperta."})
    conn.execute("UPDATE projects SET validation=? WHERE project_id=?", (j(val), project_id))
    # zapis (nie nadpisujemy decyzji eksperta); propozycje automatyczne z poprzedniego przebiegu, których już nie ma – usuwamy
    links = [l for l in links if not str(l["target_id"]).startswith("PROJ-")]
    conn.execute("DELETE FROM project_links WHERE project_id=? AND target_type='procedure' AND target_id LIKE 'PROJ-%'", (project_id,))
    keep = [l["target_id"] for l in links]
    conn.execute(f"DELETE FROM project_links WHERE project_id=? AND target_type='procedure' AND status IN ('auto','uncertain') "
                 f"AND target_id NOT IN ({','.join('?' * len(keep)) or "''"})", [project_id] + keep)
    for l in links:
        ex = one(conn, "SELECT status FROM project_links WHERE project_id=? AND target_type='procedure' AND target_id=?", (project_id, l["target_id"]))
        if ex and ex["status"] in ("approved", "rejected"):
            continue
        conn.execute("""INSERT INTO project_links (project_id,target_type,target_id,title,source_system,url,match_basis,confidence,evidence,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id,target_type,target_id) DO UPDATE SET title=excluded.title,
            match_basis=excluded.match_basis, confidence=excluded.confidence, evidence=excluded.evidence, status=excluded.status""",
                     (project_id, "procedure", l["target_id"], l["title"], "BZP", f"https://ezamowienia.gov.pl/mp-client/search/list/{l['target_id']}",
                      l["basis"], l["confidence"], j(l["evidence"]), l["status"], now_iso()))
    if not links:
        conn.execute("""INSERT OR IGNORE INTO project_links (project_id,target_type,target_id,title,source_system,match_basis,confidence,evidence,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (project_id, "missing", "BZP", "Nie znaleziono postępowania w BZP", "BZP", "brak dopasowania", 0.0,
                                              j(["brak ogłoszenia o podanym numerze i brak postępowań o podobnej nazwie dla tego NIP"]), "uncertain", now_iso()))
    else:
        conn.execute("DELETE FROM project_links WHERE project_id=? AND target_type='missing'", (project_id,))
    conn.commit()
    return links


def enrich_project(conn: sqlite3.Connection, project_id: str, live: bool = True, external: bool = True,
                   progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    from .sources import external as ext
    say = progress or (lambda m: None)
    p = one(conn, "SELECT * FROM projects WHERE project_id=?", (project_id,))
    if not p:
        raise KeyError(project_id)
    links = link_project(conn, project_id, live=live, progress=say)
    say(f"Powiązano {len(links)} postępowań BZP")
    tids = [l["target_id"] for l in links if l["status"] in ("auto", "approved")]
    org = one(conn, "SELECT * FROM organizations WHERE nip=? ORDER BY last_seen DESC LIMIT 1", (digits(p.get("org_id")),)) or {}
    results = {"bzp": len(tids), "ted": 0, "kio": 0, "bip": 0, "nik": 0, "documents": 0, "events": 0}
    kw = _keywords(p)
    conn.commit()
    if external and live:
        # TED – zamówienia powyżej progów UE
        try:
            say("Sprawdzam TED (Dziennik Urzędowy UE)…")
            short = re.split(r"\s+[–-]\s+", p.get("org_name") or "")[0]
            notices = ext.ted_search(short, limit=15)
            for n in notices:
                title = ext.ted_title(n)
                sim, rare = _title_sim(p["title"], title)
                org_st = {stem(t) for t in tokens(p.get("org_name") or "")} | {stem(t) for t in tokens(p.get("city") or "")}
                rare = [w for w in rare if w not in org_st]      # sama nazwa zamawiającego nie wystarcza
                if sim < 0.45 or not rare:
                    continue
                pubno = n.get("publication-number")
                _add_link(conn, project_id, "ted", pubno, title, "TED", f"https://ted.europa.eu/pl/notice/-/detail/{pubno}",
                          "zamawiający + podobieństwo nazwy", round(0.5 + 0.4 * sim, 2), [f"zamawiający: {short}", f"podobieństwo nazwy {sim:.0%}"],
                          "uncertain" if sim < 0.6 else "auto")
                results["ted"] += 1
            log_source(conn, "TED", f"wyszukiwanie zamawiającego {short}", "https://api.ted.europa.eu/v3/notices/search", 200, len(notices))
        except Exception as e:
            say(f"TED niedostępny: {e}")
        # KIO – orzeczenia (SAOS)
        try:
            say("Sprawdzam orzeczenia KIO (SAOS)…")
            short = re.split(r"\s+[–-]\s+", p.get("org_name") or "")[0]
            items = ext.kio_search(f'"{short}" {kw[0] if kw else ""}', limit=8)
            for it in items:
                jid = it.get("id")
                text = re.sub(r"<[^>]+>", " ", it.get("textContent") or "")
                cases = ", ".join(c.get("caseNumber", "") for c in it.get("courtCases", []))
                if fold(short) not in fold(text) and not any(k in fold(text) for k in kw[:2]):
                    continue
                doc_id = f"KIO-{jid}"
                full = ext.kio_judgment(jid)
                body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", full.get("textContent") or text))
                _store_doc(conn, doc_id, project_id, None, "KIO", "orzeczenie KIO", f"Orzeczenie {cases} z {it.get('judgmentDate')}",
                           f"https://www.saos.org.pl/judgments/{jid}", [(1, body)], meta={"case_numbers": cases})
                _add_link(conn, project_id, "document", doc_id, f"KIO {cases}", "KIO", f"https://www.saos.org.pl/judgments/{jid}",
                          "nazwa zamawiającego w treści orzeczenia", 0.6, [f"orzeczenie wymienia: {short}"], "uncertain")
                results["kio"] += 1
            log_source(conn, "KIO/SAOS", f"wyszukiwanie {short}", "https://www.saos.org.pl/api/search/judgments", 200, len(items))
        except Exception as e:
            say(f"SAOS/KIO niedostępny: {e}")
        # BIP zamawiającego – robot dokumentów
        try:
            starts = ext.bip_candidates(org.get("www"), org.get("email"), p.get("org_name") or "")
            if starts and kw:
                say(f"Przeszukuję BIP zamawiającego ({starts[0]})…")
                from . import config as _cfg
                hits = ext.crawl_site(starts[:2], kw, max_pages=int(_cfg.get()["sources"].get("bip_max_pages", 40)), progress=say)
                for h in hits[:8]:
                    doc_id = "BIP-" + h["sha256"][:16]
                    _store_doc(conn, doc_id, project_id, None, "BIP", h["kind"], h["title"][:200], h["url"], h["pages"], sha=h["sha256"], mime=h["mime"])
                    _add_link(conn, project_id, "document", doc_id, h["title"][:200], "BIP", h["url"], "słowa kluczowe inwestycji w treści",
                              round(min(0.9, 0.4 + 0.05 * h["score"]), 2), [f"trafień słów kluczowych: {h['score']}", "słowa: " + ", ".join(kw[:4])], "uncertain")
                    results["bip"] += 1
                log_source(conn, "BIP", f"przeszukanie BIP ({len(hits)} trafień)", starts[0], 200, len(hits), meta={"keywords": kw})
        except Exception as e:
            say(f"BIP: {e}")
        # NIK – kontrole dotyczące zamawiającego
        try:
            short = re.split(r"\s+[–-]\s+", p.get("org_name") or "")[0]
            core = fold(re.sub(r"^(Gmina|Miasto i Gmina|Miasto|Gmina Miejska)\s+", "", short))[:6]
            for h in [x for x in ext.nik_search(short) if core and core in fold(x["title"])][:3]:
                _add_link(conn, project_id, "nik", h["url"], h["title"][:200], "NIK", h["url"], "wyszukiwarka NIK (nazwa zamawiającego)", 0.4,
                          [f"wynik wyszukiwania: {short}"], "uncertain")
                results["nik"] += 1
        except Exception:
            pass
    # zdarzenia z dokumentów projektu
    for d in rows(conn, "SELECT doc_id FROM documents WHERE project_id=?", (project_id,)):
        results["events"] += analyze_document(conn, d["doc_id"])
    results["documents"] = conn.execute("SELECT COUNT(*) FROM documents WHERE project_id=?", (project_id,)).fetchone()[0]
    summ = project_summary(conn, project_id)
    conn.execute("UPDATE projects SET enriched_at=?, summary=? WHERE project_id=?", (now_iso(), j(summ), project_id))
    conn.commit()
    results["summary"] = summ
    say("Gotowe.")
    from .expert_policy import auto_approve
    results["auto_approved"] = auto_approve(conn)
    project_case(conn, project_id)   # przypadek projektu z aktualnymi powiązaniami i wynikiem realizacji
    if results["auto_approved"]:
        say(f"Polityka eksperta: zatwierdzono {results['auto_approved']} zdarzeń o wysokim prawdopodobieństwie.")
    return results


def _keywords(p: dict) -> list[str]:
    """Charakterystyczne słowa inwestycji (nazwy miejscowości, ulic) do przeszukiwania BIP."""
    title = p.get("title") or ""
    words = re.findall(r"\b[A-ZŁŚŻŹĆÓ][a-ząćęłńóśźż]{3,}(?:\s+[A-ZŁŚŻŹĆÓ][a-ząćęłńóśźż]{3,})?", title)
    stop = {"Budowa", "Gmina", "Gminy", "Gminie", "Przebudowa", "Rozbudowa", "Modernizacja", "Stacji", "Uzdatniania", "Wody", "Zwiększenie"}
    out = [w for w in words if w.split()[0] not in stop]
    org = re.sub(r"^(Gmina|Miasto|Gmina Miejska)\s+", "", (p.get("org_name") or "").split("–")[0]).strip()
    for k in ("kanalizac", "wodociąg", "oczyszczal", "uzdatnian", "deszczow"):
        if k in fold(title):
            out.append(k)
            break
    org_stems = {fold(t)[:5] for t in re.findall(r"\w{4,}", org)} | {fold(t)[:5] for t in re.findall(r"\w{4,}", p.get("city") or "")}
    return [w for w in dict.fromkeys(out) if fold(w)[:5] not in org_stems][:6]


def _add_link(conn, project_id, ttype, tid, title, system, url, basis, conf, ev, status):
    ex = one(conn, "SELECT status FROM project_links WHERE project_id=? AND target_type=? AND target_id=?", (project_id, ttype, tid))
    if ex and ex["status"] in ("approved", "rejected"):
        return
    conn.execute("""INSERT INTO project_links (project_id,target_type,target_id,title,source_system,url,match_basis,confidence,evidence,status,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id,target_type,target_id) DO UPDATE SET confidence=excluded.confidence, evidence=excluded.evidence""",
                 (project_id, ttype, tid, title, system, url, basis, conf, j(ev), status, now_iso()))
    conn.commit()   # krótkie transakcje – nie blokujemy bazy podczas pobierania z sieci


def _store_doc(conn, doc_id, project_id, tender_id, system, dtype, title, url, pages, sha=None, mime=None, local_path=None, meta=None):
    text = full_text(pages)
    conn.execute("""INSERT INTO documents (doc_id,project_id,tender_id,source_system,doc_type,title,url,local_path,fetched_at,sha256,mime,pages,chars,text_z,meta,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET project_id=COALESCE(excluded.project_id,project_id),
        text_z=excluded.text_z, fetched_at=excluded.fetched_at, pages=excluded.pages, chars=excluded.chars""",
                 (doc_id, project_id, tender_id, system, dtype, title, url, local_path, now_iso(), sha or hashlib.sha256(text.encode()).hexdigest(),
                  mime, len(pages), len(text), zpack(j([[n, t] for n, t in pages])), j(meta or {}), "new"))
    conn.commit()


def import_document(conn: sqlite3.Connection, filename: str, data: bytes, project_id: str | None = None, tender_id: str | None = None,
                    uploads_dir: Path | None = None) -> dict[str, Any]:
    parsed = read_any(filename, data)
    sha = hashlib.sha256(data).hexdigest()
    doc_id = "DOC-" + sha[:16]
    local = None
    if uploads_dir:
        uploads_dir.mkdir(parents=True, exist_ok=True)
        local = uploads_dir / f"{sha[:16]}_{re.sub(r'[^A-Za-z0-9._-]+', '_', filename)[-80:]}"
        local.write_bytes(data)
    _store_doc(conn, doc_id, project_id, tender_id, "PLIK", _doc_type(filename, full_text(parsed["pages"])), filename, None, parsed["pages"], sha=sha,
               local_path=str(local) if local else None)
    if project_id:
        _add_link(conn, project_id, "document", doc_id, filename, "PLIK", None, "dokument wgrany przez użytkownika", 1.0, ["wgrany ręcznie"], "approved")
    n_ev = analyze_document(conn, doc_id)
    from .expert_policy import auto_approve
    auto_approve(conn, "doc_id=?", (doc_id,))
    log_source(conn, "PLIK", "wgranie dokumentu", filename, 200, 1, sha256=sha)
    conn.commit()
    return {"doc_id": doc_id, "pages": len(parsed["pages"]), "chars": len(full_text(parsed["pages"])), "events": n_ev, "kind": parsed["kind"]}


# zdanie z rekordu jest zdarzeniem tylko, gdy opisuje, że coś SIĘ STAŁO (nie: sama kwota dofinansowania czy cel projektu)
EVENT_SIGNAL = (r"aneks|przedluz|wydluz|opozni|zmian\w* (umowy|terminu|zakresu|wynagrodz)|kolizj|robot\w* (dodatkow|zamienn|nieprzewidz)|"
                r"uniewazni|odstapi|rozwiaz\w* umow|kar\w* umown|\bspor(?!t)|\bsad(?!z)|\bkio\b|odwola|niewykona|nienalezyt|wzrost\w* (koszt|cen|wartosc)|"
                r"zwieksz\w* (wynagrodz|wartosc|koszt)|przekrocz|wstrzyma|problem|trudnos|awari|upadlos|zejscie wykonawcy|nie (uzyskano|zawarto)|brak\w* (zgod|decyzj|pozwol)")


def project_case_id(project_id: str) -> str:
    return "PROJ-" + re.sub(r"[^A-Za-z0-9._-]", "_", project_id)


def project_case(conn: sqlite3.Connection, project_id: str) -> str | None:
    """Projekt z rekordu jako przypadek w bazie doświadczeń (wyszukiwany jak postępowania): treść rekordu, cechy,
    wynik realizacji z powiązanego postępowania BZP albo z treści rekordu (np. „przedłużono termin o 120 dni”)
    oraz zdarzenia ryzyka z rekordu i z powiązanych postępowań. Dzięki temu nowo zaimportowany projekt od razu
    trafia do grupy porównawczej podobnych inwestycji."""
    from .build import stem_string
    from .classify import extract_connections, extract_length_km
    p = one(conn, "SELECT * FROM projects WHERE project_id=?", (project_id,))
    if not p:
        return None
    cid = project_case_id(project_id)
    clf = Classifier(conn)
    ann = parse_annotation(p.get("annotation"))
    desc = " ".join(x for x in (ann.get("description"), p.get("scope"), p.get("annotation")) if x)
    text = (p.get("title") or "") + ". " + desc
    sub, extra = clf.classify_subsector(p.get("title") or "", desc)
    from .query import sector_from_text
    sector = "wod-kan" if sub else (sector_from_text(text, sub)[0] or "inne")
    links = rows(conn, "SELECT target_id FROM project_links WHERE project_id=? AND target_type='procedure' AND status IN ('auto','approved')", (project_id,))
    linked = [one(conn, "SELECT * FROM procedures WHERE tender_id=?", (l["target_id"],)) for l in links]
    linked = [x for x in linked if x]
    best = max(linked, key=lambda x: (x.get("has_outcome") or 0, x.get("contract_value") or 0), default={})
    rec_ev = rows(conn, "SELECT risk_code, effect_time_days FROM risk_events WHERE doc_id=? AND status!='rejected'", ("REC-" + re.sub(r"[^A-Za-z0-9._-]", "_", project_id),))
    codes = set(unj(best.get("risk_codes"), []) or []) | {e["risk_code"] for e in rec_ev}
    rec_delay = max((e["effect_time_days"] or 0 for e in rec_ev), default=0) or None
    delayed = best.get("delayed")
    delay_days = best.get("delay_days")
    if rec_delay and not delay_days:
        delayed, delay_days = 1, rec_delay
    year = int(p["record_date"][:4]) if (p.get("record_date") or "")[:4].isdigit() else None
    vals = {
        "tender_id": cid, "source_system": "REKORD", "sector": sector, "subsector": sub, "subsectors_extra": j(extra),
        "works_type": clf.works_type(p.get("title") or "") or clf.works_type(desc), "design_build": 1 if clf.design_build(text) else 0,
        "title": p.get("title"), "description": desc[:4000], "org_key": p.get("org_id"), "org_name": p.get("org_name"), "nip": p.get("org_id"),
        "province": p.get("province"), "city": p.get("city"), "estimated_value_net": p.get("project_value"),
        "planned_days": best.get("planned_days"), "year": year or best.get("year"), "length_km": extract_length_km(text) or best.get("length_km"),
        "connections": extract_connections(text) or best.get("connections"), "features": j(clf.features_of(text)),
        "n_notices": best.get("n_notices"), "n_contracts": best.get("n_contracts"), "n_changes": best.get("n_changes"),
        "contract_value": best.get("contract_value"), "paid_value": best.get("paid_value"), "cost_change_pct": best.get("cost_change_pct"),
        "actual_days": best.get("actual_days"), "delay_days": delay_days, "delayed": delayed, "has_outcome": 1,
        "status": "projekt z rekordu" + (" (powiązany z BZP)" if best else ""), "risk_codes": j(sorted(codes)), "updated_at": now_iso(),
    }
    cols = list(vals)
    conn.execute(f"INSERT INTO procedures ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) ON CONFLICT(tender_id) DO UPDATE SET "
                 + ",".join(f"{c}=excluded.{c}" for c in cols if c != "tender_id"), [vals[c] for c in cols])
    conn.execute("UPDATE risk_events SET tender_id=? WHERE doc_id=?", (cid, "REC-" + re.sub(r"[^A-Za-z0-9._-]", "_", project_id)))
    old = conn.execute("SELECT fts_rowid FROM fts_map WHERE tender_id=?", (cid,)).fetchone()
    if old:
        conn.execute("DELETE FROM procedures_fts WHERE rowid=?", (old[0],))
    cur = conn.execute("INSERT INTO procedures_fts (tender_id,title_s,desc_s,changes_s) VALUES (?,?,?,?)",
                       (cid, stem_string(p.get("title")), stem_string(desc[:4000]), ""))
    conn.execute("INSERT OR REPLACE INTO fts_map VALUES (?,?)", (cid, cur.lastrowid))
    conn.commit()
    return cid


def record_events(conn: sqlite3.Connection, project_id: str) -> int:
    """Wiedza wpisana w rekord projektu (adnotacja instytucji, zakres, opis) -> dokument „rekord projektu” + zdarzenia ryzyka
    (z cytatem i wskazaniem kolumny). Wpisy instytucji traktujemy jako wiarygodne: niższy próg rozpoznania, bez obniżania pewności."""
    p = one(conn, "SELECT * FROM projects WHERE project_id=?", (project_id,))
    if not p:
        return 0
    text = "\n".join(x for x in (p.get("annotation"), p.get("scope")) if x and str(x).strip())
    if len(text.strip()) < 20:
        return 0
    doc_id = "REC-" + re.sub(r"[^A-Za-z0-9._-]", "_", project_id)
    _store_doc(conn, doc_id, project_id, None, "REKORD", "rekord projektu", f"Rekord projektu {project_id} – adnotacja instytucji i zakres",
               None, [(1, text)], meta={"źródło": p.get("source_file"), "wersja": p.get("record_version"), "data": p.get("record_date")})
    _add_link(conn, project_id, "document", doc_id, f"Rekord projektu {project_id} – adnotacja instytucji", "REKORD", None,
              "treść rekordu projektu", 1.0, [f"plik: {p.get('source_file') or ''}"], "approved")
    # wpis instytucji jest źródłem wiarygodniejszym niż strona WWW: niższy próg rozpoznania, premia za źródło (maks. 0,95)
    return analyze_document(conn, doc_id, min_conf=0.5, factor=1.1, event_type="zdarzenie wskazane w rekordzie projektu",
                            method="reguła (katalog ekspercki) – treść rekordu projektu", require=EVENT_SIGNAL)


def _doc_type(filename: str, text: str) -> str:
    f = fold(filename + " " + text[:3000])
    for name, rx in (("aneks do umowy", r"aneks"), ("umowa", r"\bumowa nr|umowa o "), ("SWZ", r"specyfikacj\w* warunkow zamowienia|\bswz\b"),
                     ("PFU", r"program\w* funkcjonalno"), ("protokół", r"protokol"), ("wystąpienie pokontrolne", r"wystapien\w* pokontroln"),
                     ("orzeczenie KIO", r"krajowa izba odwolawcza"), ("uchwała", r"uchwal"), ("zarządzenie", r"zarzadzeni"), ("raport", r"raport|sprawozdan")):
        if re.search(rx, f):
            return name
    return "dokument"


def analyze_document(conn: sqlite3.Connection, doc_id: str, min_conf: float = 0.75, factor: float = 0.9,
                     event_type: str = "fragment dokumentu wskazujący na ryzyko", method: str = "reguła (katalog ekspercki) – dokument",
                     require: str | None = None) -> int:
    """Znajduje w dokumencie fragmenty świadczące o ryzyku (zdania dopasowane do katalogu) – z numerem strony."""
    from .textutil import zunpack
    d = one(conn, "SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    if not d:
        return 0
    pages = unj(zunpack(d["text_z"]), []) or []
    if doc_id.startswith("REC-") and require is None:
        # treść rekordu projektu – zawsze te same zasady (także przy ponownej analizie i wzbogacaniu)
        min_conf, factor, require = 0.5, 1.1, EVENT_SIGNAL
        event_type, method = "zdarzenie wskazane w rekordzie projektu", "reguła (katalog ekspercki) – treść rekordu projektu"
    clf = Classifier(conn)
    conn.execute("DELETE FROM risk_events WHERE doc_id=? AND status='pending'", (doc_id,))
    n = 0
    # zdania już zapisane w innych dokumentach projektu (menu i stopki BIP powtarzają się na każdej stronie)
    seen = {(r[0], fold(r[1] or "")[:80]) for r in conn.execute(
        "SELECT risk_code, quote FROM risk_events WHERE project_id=? AND doc_id IS NOT NULL AND doc_id!=?", (d.get("project_id"), doc_id))} if d.get("project_id") else set()
    for pno, text in pages:
        for sent in split_sentences(text):
            if len(sent) > 700:
                sent = sent[:700]
            if _boilerplate(sent) or (require and not re.search(require, fold(sent))):
                continue
            # w dokumentach wymagamy co najmniej dwóch niezależnych sygnałów (mniej fałszywych trafień)
            codes = [c for c in clf.classify_risk(sent, max_codes=1) if c["confidence"] >= min_conf and c["status"] == "active"]
            if not codes:
                continue
            key = (codes[0]["code"], fold(sent)[:80])
            if key in seen:
                continue
            seen.add(key)
            c = codes[0]
            conn.execute("""INSERT INTO risk_events (tender_id,project_id,doc_id,event_type,risk_code,cause,effect_types,effect_time_days,quote,
                source_system,source_ref,source_url,locator,matched_terms,confidence,method,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (d.get("tender_id"), d.get("project_id"), doc_id, event_type, c["code"], sent[:500],
                          j(clf.effect_types(sent)), time_extension_days(sent), sent, d.get("source_system"), d.get("title"), d.get("url"),
                          f"strona {pno}" if not doc_id.startswith("REC-") else "rekord projektu – kolumna „Adnotacja instytucji” / zakres",
                          j(c["terms"]), round(min(0.95, c["confidence"] * factor), 2), method, "pending", now_iso()))
            n += 1
            if n >= 60:
                break
    conn.execute("UPDATE documents SET status='analyzed' WHERE doc_id=?", (doc_id,))
    conn.commit()
    return n


_BOILER = re.compile(r"pouczeni|zrzec|zrzeczeni|klauzul\w* informacyjn|\brodo\b|danych osobowych|inspektor\w* ochrony|przejdz do|deklaracj\w* dostepnosci|"
                     r"mapa serwisu|polityk\w* prywatnosci|cookies|metryka|wersja do druku|liczba odslon")


def _boilerplate(sent: str) -> bool:
    """Menu, stopki i pouczenia BIP – nie są dowodem zdarzenia."""
    f = fold(sent)
    if _BOILER.search(f):
        return True
    words = re.findall(r"[^\W\d_]{3,}", sent)
    if len(words) >= 12:
        caps = sum(1 for w in words if w[0].isupper())
        if caps / len(words) > 0.45:   # listy nazw (menu, nawigacja)
            return True
    return False


def project_summary(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    links = rows(conn, "SELECT * FROM project_links WHERE project_id=? AND status!='rejected'", (project_id,))
    # podsumowanie tylko z powiązań pewnych lub zatwierdzonych przez eksperta (niepewne czekają na decyzję)
    tids = [l["target_id"] for l in links if l["target_type"] == "procedure" and l["status"] in ("auto", "approved")]
    procs = rows(conn, f"SELECT * FROM procedures WHERE tender_id IN ({','.join('?' * len(tids))})", tids) if tids else []
    ev = rows(conn, f"SELECT risk_code, status FROM risk_events WHERE (tender_id IN ({','.join('?' * len(tids))}) OR project_id=?) AND status!='rejected'",
              tids + [project_id]) if tids else rows(conn, "SELECT risk_code, status FROM risk_events WHERE project_id=? AND status!='rejected'", (project_id,))
    codes: dict[str, int] = {}
    for e in ev:
        if e["risk_code"] != "BRAK":
            codes[e["risk_code"]] = codes.get(e["risk_code"], 0) + 1
    main = procs[0] if procs else {}
    gaps = completeness(conn, project_id, procs)
    return {
        "gaps": gaps,
        "procedures": len(procs), "contracts": sum(p.get("n_contracts") or 0 for p in procs), "changes": sum(p.get("n_changes") or 0 for p in procs),
        "contract_value": sum(p.get("contract_value") or 0 for p in procs) or None, "paid_value": sum(p.get("paid_value") or 0 for p in procs) or None,
        "cost_change_pct": main.get("cost_change_pct"), "delayed": max((p.get("delayed") or 0 for p in procs), default=None) if procs else None,
        "delay_days": max((p.get("delay_days") or 0 for p in procs), default=None) if procs else None,
        "risk_codes": codes, "events": sum(codes.values()), "pending_events": sum(1 for e in ev if e["status"] == "pending"),
        "uncertain_links": sum(1 for l in links if l["status"] == "uncertain"),
        "status": main.get("status"),
    }


def project_record(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """Pełny rekord projektu do eksportu – każde pole prowadzi do źródła."""
    from .textutil import zunpack
    p = one(conn, "SELECT * FROM projects WHERE project_id=?", (project_id,))
    if not p:
        raise KeyError(project_id)
    p["raw"] = unj(p.get("raw"), {})
    p["validation"] = unj(p.get("validation"), [])
    p["summary"] = unj(p.get("summary"), {})
    links = rows(conn, "SELECT * FROM project_links WHERE project_id=? ORDER BY confidence DESC", (project_id,))
    for l in links:
        l["evidence"] = unj(l.get("evidence"), [])
    tids = [l["target_id"] for l in links if l["target_type"] == "procedure" and l["status"] != "rejected"]
    procedures = []
    for tid in tids:
        pr = one(conn, "SELECT * FROM procedures WHERE tender_id=?", (tid,))
        if not pr:
            continue
        pr["features"] = unj(pr.get("features"), [])
        pr["subsectors_extra"] = unj(pr.get("subsectors_extra"), [])
        pr["risk_codes"] = unj(pr.get("risk_codes"), [])
        pr["notices"] = rows(conn, "SELECT notice_number, notice_type, pub_date, title, url, fetched_at, source_system FROM notices WHERE tender_id=? ORDER BY pub_date", (tid,))
        pr["contracts"] = rows(conn, "SELECT * FROM contracts WHERE tender_id=?", (tid,))
        pr["changes"] = rows(conn, "SELECT * FROM changes WHERE tender_id=? ORDER BY change_date, change_id", (tid,))
        for ch in pr["changes"]:
            ch["effect_types"] = unj(ch.get("effect_types"), [])
            ch["actions"] = unj(ch.get("actions"), [])
        procedures.append(pr)
    events = rows(conn, f"SELECT * FROM risk_events WHERE project_id=? OR tender_id IN ({','.join('?' * len(tids)) or 'NULL'}) ORDER BY confidence DESC",
                  [project_id] + tids)
    cat = {r["code"]: r["name"] for r in rows(conn, "SELECT code, name FROM risk_catalog")}
    for e in events:
        e["risk_name"] = cat.get(e["risk_code"], "do klasyfikacji")
        e["effect_types"] = unj(e.get("effect_types"), [])
        e["actions"] = unj(e.get("actions"), [])
        e["matched_terms"] = unj(e.get("matched_terms"), [])
    docs = rows(conn, "SELECT doc_id, source_system, doc_type, title, url, fetched_at, sha256, pages, chars, status FROM documents WHERE project_id=?", (project_id,))
    history = rows(conn, "SELECT * FROM expert_log WHERE entity_id=? OR entity_id IN (SELECT CAST(event_id AS TEXT) FROM risk_events WHERE project_id=?) ORDER BY ts",
                   (project_id, project_id))
    return {"schema": "riskminer.project_record/1.0", "exported_at": now_iso(), "project": p, "links": links, "procedures": procedures,
            "risk_events": events, "documents": docs, "expert_history": history}


def completeness(conn: sqlite3.Connection, project_id: str, procs: list[dict]) -> list[dict[str, str]]:
    """Braki w pakiecie danych projektu: czego nie ma, dlaczego to ważne i skąd to uzupełnić.
    Brak dokumentu w pakiecie ≠ brak zdarzenia w inwestycji (zgodnie z materiałem eksperckim MFiPR)."""
    out = []
    if not procs:
        return [{"item": "Postępowanie w BZP", "why": "bez postępowania nie da się odtworzyć przebiegu (umowy, zmiany, wykonanie)",
                 "where": "BZP / platforma zakupowa zamawiającego; sprawdź numer ogłoszenia lub NIP w rekordzie", "level": "error"}]
    for p in procs:
        types = {r[0] for r in conn.execute("SELECT DISTINCT notice_type FROM notices WHERE tender_id=?", (p["tender_id"],))}
        t = (p.get("title") or "")[:60]
        if "ContractNotice" not in types:
            out.append({"item": f"Ogłoszenie o zamówieniu ({t})", "why": "brak szacunku wartości, opisu przedmiotu, okresu realizacji i projektowanych zmian umowy",
                        "where": "BZP (numer przywołany w ogłoszeniu o wyniku/wykonaniu) lub TED", "level": "warning"})
        if "TenderResultNotice" not in types:
            out.append({"item": f"Ogłoszenie o wyniku postępowania ({t})", "why": "brak liczby i cen ofert, wykonawcy i daty umowy",
                        "where": "BZP", "level": "warning"})
        if "ContractPerformingNotice" not in types:
            out.append({"item": f"Ogłoszenie o wykonaniu umowy ({t})", "why": "nie wiadomo, czy umowę wykonano w terminie i należycie oraz ile wypłacono "
                        "(umowa może być w realizacji – monitoring sprawdzi nowe ogłoszenia)", "where": "BZP (publikacja do 30 dni po wykonaniu umowy)", "level": "info"})
        if (p.get("n_changes") or 0) > 0 and "AgreementUpdateNotice" not in types:
            out.append({"item": f"Aneksy / ogłoszenia o zmianie umowy ({t})", "why": "zmiany znane tylko z ogłoszenia o wykonaniu – pełne uzasadnienie bywa w aneksach",
                        "where": "BIP zamawiającego (rejestr umów), wniosek o informację publiczną", "level": "info"})
    docs = {r[0] for r in conn.execute("SELECT doc_type FROM documents WHERE project_id=?", (project_id,))}
    if not docs & {"umowa", "aneks do umowy"}:
        out.append({"item": "Podpisana umowa z załącznikami", "why": "warunki kontraktowe (kary, zmiany, odbiory) – rzadko publikowana; można odtworzyć z PPU/SWU "
                    "z oznaczeniem „warunki kontraktowe odtworzone; brak podpisanego egzemplarza umowy”", "where": "BIP (rejestr umów), platforma zakupowa (PPU)", "level": "info"})
    if not docs & {"SWZ", "PFU"}:
        out.append({"item": "SWZ / PFU / OPZ", "why": "źródło ryzyk na etapie przygotowania (zakres, wymagania, podział odpowiedzialności)",
                    "where": f"platforma zakupowa: {procs[0].get('platform_url') or 'adres w ogłoszeniu o zamówieniu'}", "level": "info"})
    return out
