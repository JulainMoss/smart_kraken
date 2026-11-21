"""Raport dla nowej inwestycji: grupa porównawcza, prognozy kosztu i czasu z przedziałami i liczebnością próby,
czerwone flagi, najczęstsze problemy z dowodami źródłowymi, rekomendacje, propozycje zapisów umownych,
lista kontrolna braków w opisie oraz osoby do kontaktu.

Każda sekcja ma oznaczenie rodzaju informacji:
  DANE   – dane źródłowe (ogłoszenia, dokumenty) z linkiem,
  OBLICZ – obliczenia statystyczne na danych (z N i przedziałem),
  WNIOSEK – wnioski/rekomendacje (reguły eksperckie lub model językowy) – wymagają oceny eksperta.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import statistics
import urllib.parse
import uuid
from typing import Any

from . import llm
from .db import j, rows, setting, unj
from .query import PROVINCES
from .search import VAT, candidate_value, search
from .textutil import fmt_pln, fold, quantile, wilson

KIND = {"DANE": "dane źródłowe", "OBLICZ": "obliczenia na danych", "WNIOSEK": "wniosek / rekomendacja"}


def _pct(x: float | None, digits: int = 0) -> str:
    if x is None:
        return "b.d."
    return f"{x * 100:.{digits}f}%".replace(".", ",")


def _days_txt(d: float | None) -> str:
    if d is None:
        return "b.d."
    d = int(round(d))
    if d >= 60:
        return f"{d} dni (≈{str(round(d / 30.4375, 1)).replace('.', ',')} mies.)"
    return f"{d} dni"


def _sg(x: float | None) -> str:
    return "b.d." if x is None else f"{x:+.1f}%".replace(".", ",")


def _stats(vals: list[float]) -> dict[str, Any]:
    v = [x for x in vals if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "p10": quantile(v, 0.10), "p25": quantile(v, 0.25), "median": quantile(v, 0.5), "p75": quantile(v, 0.75),
            "p90": quantile(v, 0.90), "mean": statistics.fmean(v), "min": min(v), "max": max(v)}


def _trim(vals: list[float], lo: float, hi: float) -> list[float]:
    return [x for x in vals if x is not None and lo <= x <= hi]


def analyze(conn: sqlite3.Connection, q: dict[str, Any], title: str | None = None, use_llm: bool = True,
            exclude_tenders: set[str] | None = None) -> dict[str, Any]:
    P = {k: v.get("value") for k, v in q["params"].items()}
    found = search(conn, q, exclude_tenders=exclude_tenders)
    min_n = setting(conn, "search.min_sample_warning", 8)
    # grupa porównawcza: wyniki wystarczająco bliskie najlepszemu
    group = []
    if found:
        best = found[0]["score"]
        group = [r for r in found if r["score"] >= 0.6 * best] or found[:5]
        if len(group) < 5:
            group = found[:min(len(found), 8)]
    tids = [r["tender_id"] for r in group]
    procs = [r["proc"] for r in group]
    n = len(procs)

    # ---------------- zdarzenia ryzyka grupy
    ev = []
    if tids:
        ev = rows(conn, "SELECT e.*, p.title AS case_title, p.org_name AS case_org FROM risk_events e JOIN procedures p ON p.tender_id=e.tender_id "
                        f"WHERE e.tender_id IN ({','.join('?' * len(tids))}) AND e.status!='rejected'", tids)
    cat = {r["code"]: r for r in rows(conn, "SELECT * FROM risk_catalog")}
    # ---------------- prognozy: czas
    delayed_flags = [p["delayed"] for p in procs if p.get("delayed") is not None]
    k_del = sum(1 for x in delayed_flags if x)
    n_del = len(delayed_flags)
    lo, hi = wilson(k_del, n_del)
    actual = [p["actual_days"] for p in procs if p.get("actual_days")]
    planned = [p["planned_days"] for p in procs if p.get("planned_days")]
    delays = [p["delay_days"] for p in procs if p.get("delay_days")]
    ratio = [p["actual_days"] / p["planned_days"] for p in procs if p.get("actual_days") and p.get("planned_days") and p["planned_days"] > 14]
    ratio = _trim(ratio, 0.2, 5.0)
    user_days = P.get("duration_days")
    time_fc: dict[str, Any] = {
        "kind": "OBLICZ",
        "delay_probability": k_del / n_del if n_del else None, "delay_ci90": [lo, hi], "delay_k": k_del, "delay_n": n_del,
        "planned_days": _stats(planned), "actual_days": _stats(actual), "delay_days_when_late": _stats(delays), "ratio_actual_to_planned": _stats(ratio),
        "user_planned_days": user_days,
    }
    if user_days and ratio:
        time_fc["expected_days_range"] = [user_days * (quantile(ratio, 0.5) or 1), user_days * (quantile(ratio, 0.8) or 1)]
    # ---------------- prognozy: koszt
    cost_changes = _trim([p["cost_change_pct"] for p in procs if p.get("cost_change_pct") is not None], -60, 150)
    increased = [x for x in cost_changes if x > 0.5]
    budget_gross = P.get("budget")
    if budget_gross and (P.get("budget_vat") == "netto"):
        budget_gross = budget_gross * VAT
    cost_fc: dict[str, Any] = {"kind": "OBLICZ", "change_pct": _stats(cost_changes), "share_increase": (len(increased) / len(cost_changes)) if cost_changes else None,
                               "share_increase_ci90": list(wilson(len(increased), len(cost_changes))) if cost_changes else None,
                               "user_budget_gross": budget_gross}
    if budget_gross and cost_changes:
        cost_fc["expected_final_range"] = [budget_gross * (1 + (quantile(cost_changes, 0.5) or 0) / 100),
                                           budget_gross * (1 + (quantile(cost_changes, 0.9) or 0) / 100)]
    # koszt jednostkowy (zł brutto / km) – ocena kosztorysu
    per_km = [candidate_value(p) / p["length_km"] for p in procs if candidate_value(p) and p.get("length_km") and p["length_km"] >= 0.2]
    per_km = _trim(per_km, 50_000, 20_000_000)
    values = [candidate_value(p) for p in procs if candidate_value(p)]
    budget_check: dict[str, Any] = {"kind": "OBLICZ", "per_km": _stats(per_km), "values": _stats(values)}
    verdict_budget = None
    if budget_gross and P.get("length_km") and per_km and len(per_km) >= 3:
        user_per_km = budget_gross / P["length_km"]
        budget_check["user_per_km"] = user_per_km
        pos = sum(1 for x in per_km if x <= user_per_km) / len(per_km)
        budget_check["user_percentile"] = pos
        budget_check["typical_budget_range"] = [quantile(per_km, 0.25) * P["length_km"], quantile(per_km, 0.75) * P["length_km"]]
        verdict_budget = "niski" if pos < 0.2 else "wysoki" if pos > 0.85 else "typowy"
    elif budget_gross and values and len(values) >= 3:
        pos = sum(1 for x in values if x <= budget_gross) / len(values)
        budget_check["user_percentile_total"] = pos
    budget_check["verdict"] = verdict_budget
    # przetarg: ceny ofert vs szacunek, konkurencja, unieważnienia
    tender_rows = []
    if tids:
        tender_rows = rows(conn, f"SELECT tender_id, contract_value, estimated_value_net, offers FROM contracts WHERE tender_id IN ({','.join('?' * len(tids))})", tids)
    price_vs_est = _trim([c["contract_value"] / (c["estimated_value_net"] * VAT) * 100 - 100 for c in tender_rows
                          if c.get("contract_value") and c.get("estimated_value_net")], -70, 200)
    offers = [p["offers_avg"] for p in procs if p.get("offers_avg")]
    # unieważnienia liczymy szerzej: wszystkie podobne postępowania (także bez umowy)
    cancel = _cancellations(conn, q, exclude_tenders)
    tender_fc = {"kind": "OBLICZ", "price_vs_estimate_pct": _stats(price_vs_est), "offers": _stats(offers), "cancellations": cancel}

    # ---------------- ryzyka (częstość w grupie + dowody)
    by_code: dict[str, dict[str, Any]] = {}
    for e in ev:
        code = e["risk_code"]
        if code == "BRAK":
            continue
        d = by_code.setdefault(code, {"code": code, "tenders": set(), "events": [], "approved": 0, "time": [], "cost": []})
        d["tenders"].add(e["tender_id"])
        d["events"].append(e)
        if e["status"] in ("approved", "corrected"):
            d["approved"] += 1
        if e.get("effect_time_days"):
            d["time"].append(e["effect_time_days"])
        if e.get("effect_cost_pln"):
            d["cost"].append(e["effect_cost_pln"])
    risks = []
    for code, d in by_code.items():
        c = cat.get(code, {})
        k = len(d["tenders"])
        l, h = wilson(k, n)
        evs = sorted(d["events"], key=lambda e: (e["status"] not in ("approved", "corrected"), -(e.get("exemplary") or 0), -(e.get("confidence") or 0)))
        examples, seen_t = [], set()
        for e in evs:
            if e["tender_id"] in seen_t or not e.get("quote"):
                continue
            seen_t.add(e["tender_id"])
            examples.append({"quote": e["quote"][:420], "source_ref": e["source_ref"], "source_url": e["source_url"], "locator": e["locator"],
                             "case_title": e["case_title"], "case_org": e["case_org"], "status": e["status"], "event_id": e["event_id"],
                             "confidence": e["confidence"], "event_type": e["event_type"], "actions": unj(e.get("actions"), []),
                             "action_effectiveness": e.get("action_effectiveness")})
            if len(examples) >= 4:
                break
        actions: dict[str, int] = {}
        for e in d["events"]:
            for a in unj(e.get("actions"), []) or []:
                actions[a] = actions.get(a, 0) + 1
        risks.append({
            "kind": "OBLICZ+DANE", "code": code, "name": c.get("name", code), "catalog_status": c.get("status"), "effects": c.get("effects"),
            "cases": k, "share": k / n if n else 0, "ci90": [l, h], "events": len(d["events"]), "approved_events": d["approved"],
            "median_time_days": statistics.median(d["time"]) if d["time"] else None,
            "median_cost_pln": statistics.median(d["cost"]) if d["cost"] else None,
            "examples": examples, "actions_seen": sorted(actions.items(), key=lambda kv: -kv[1])[:5],
            "mitigations": unj(c.get("mitigations"), []), "clauses": unj(c.get("clauses"), []),
        })
    risks.sort(key=lambda r: (-r["share"], -r["events"]))

    # ---------------- lista kontrolna – czego brakuje w opisie
    checklist = []
    ftext = fold(q.get("text", ""))
    for item in setting(conn, "checklist", []):
        present = bool(re.search(item["detect"], ftext)) if item.get("detect") else False
        checklist.append({"code": item["code"], "text": item["text"], "ref": item.get("ref"), "present": present})

    # ---------------- czerwone flagi
    flags = _flags(P, time_fc, cost_fc, budget_check, tender_fc, risks, checklist, n)
    # ---------------- rekomendacje
    recs = _recommendations(risks, checklist, P)
    # ---------------- kontakty
    contacts = _contacts(conn, group)
    low_data = n < min_n or (group and group[0]["score"] < 0.45)
    comparables = [_case_card(conn, r) for r in group]
    others = [_case_card(conn, r, light=True) for r in found if r not in group][:10]

    rep: dict[str, Any] = {
        "analysis_id": "AN-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4],
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "title": title or _auto_title(q),
        "query": q,
        "sample": {"n": n, "candidates": len(found), "min_recommended": min_n, "low_data": bool(low_data),
                   "db_completed_cases": conn.execute("SELECT COUNT(*) FROM procedures WHERE has_outcome=1").fetchone()[0],
                   "similarity_top": group[0]["score"] if group else None},
        "forecast": {"time": time_fc, "cost": cost_fc, "budget": budget_check, "tender": tender_fc},
        "flags": flags, "risks": risks, "checklist": checklist, "recommendations": recs,
        "comparables": comparables, "other_similar": others, "contacts": contacts,
        "kinds": KIND,
        "method": {"query": q.get("method"), "search": "BM25 (FTS5, stemming PL) + podobieństwo cech; wagi: ustawienia eksperckie",
                   "risk": "reguły katalogu eksperckiego R01–R15 (+ propozycje N01–N03); zdarzenia zatwierdzone przez eksperta mają pierwszeństwo",
                   "stats": "kwantyle empiryczne; przedziały Wilsona 90% dla udziałów; wartości skrajne obcięte (koszt −60%…+150%)"},
    }
    rep["summary"] = plain_summary(rep)
    if use_llm and llm.enabled():
        try:
            rep["llm_summary"] = {"kind": "WNIOSEK", "model": llm.cfg().get("model"),
                                  "text": llm.summarize_report({"podsumowanie": rep["summary"], "flagi": [f["text"] for f in flags],
                                                               "ryzyka": [(r["name"], round(r["share"] * 100)) for r in risks[:6]], "N": n})}
        except Exception as e:
            rep["llm_summary"] = {"kind": "WNIOSEK", "error": f"Model niedostępny: {type(e).__name__}"}
    return rep


def _auto_title(q: dict) -> str:
    txt = q.get("text", "")
    m = re.search(r"(?:pn\.|pod nazwą|nazwie|zadanie|zadania|inwestycj\w*)\s*[:]?\s*[„\"]([^”\"]{12,160})[”\"]", txt)
    if m:
        return m.group(1).strip()
    t = txt.strip().split("\n")[0]
    return (t[:110] + "…") if len(t) > 110 else t or "Analiza nowej inwestycji"


def _cancellations(conn: sqlite3.Connection, q: dict, exclude: set[str] | None) -> dict[str, Any]:
    from .search import search as _s
    res = _s(conn, q, top_k=40, only_completed=False, exclude_tenders=exclude)
    if not res:
        return {"n": 0}
    tids = [r["tender_id"] for r in res]
    rs = rows(conn, f"SELECT tender_id, cancelled_parts, parts FROM procedures WHERE tender_id IN ({','.join('?' * len(tids))}) AND parts>0", tids)
    n = len(rs)
    k = sum(1 for r in rs if r["cancelled_parts"])
    budget_reason = conn.execute(f"SELECT COUNT(DISTINCT tender_id) FROM risk_events WHERE event_type='unieważnienie postępowania' AND risk_code='R13' "
                                 f"AND tender_id IN ({','.join('?' * len(tids))})", tids).fetchone()[0]
    l, h = wilson(k, n)
    return {"n": n, "k": k, "share": k / n if n else None, "ci90": [l, h], "over_budget": budget_reason}


def _flags(P, time_fc, cost_fc, budget, tender, risks, checklist, n) -> list[dict[str, Any]]:
    flags = []

    def add(level, text, basis, kind="OBLICZ"):
        flags.append({"level": level, "text": text, "basis": basis, "kind": kind})

    if n < 5:
        add("amber", "Mało podobnych zakończonych inwestycji w bazie – wyniki orientacyjne.", f"N = {n}")
    dp = time_fc.get("delay_probability")
    if dp is not None and time_fc["delay_n"] >= 5 and dp >= 0.4:
        add("red" if dp >= 0.55 else "amber", f"Wysokie ryzyko opóźnienia: {_pct(dp)} podobnych umów zakończono po terminie.",
            f"{time_fc['delay_k']} z {time_fc['delay_n']} umów; 90% CI {_pct(time_fc['delay_ci90'][0])}–{_pct(time_fc['delay_ci90'][1])}")
    ud = P.get("duration_days")
    act = time_fc.get("actual_days", {})
    if ud and act.get("n", 0) >= 4 and act.get("p25") and ud < act["p25"]:
        add("red", f"Planowany czas ({_days_txt(ud)}) jest krótszy niż w 75% podobnych realizacji (rzeczywisty czas: mediana {_days_txt(act['median'])}).",
            f"rzeczywiste czasy N={act['n']}")
    elif ud and act.get("n", 0) >= 4 and act.get("median") and ud < act["median"]:
        add("amber", f"Planowany czas ({_days_txt(ud)}) jest krótszy niż mediana rzeczywistego czasu podobnych realizacji ({_days_txt(act['median'])}).",
            f"N={act['n']}")
    if budget.get("verdict") == "niski":
        add("red", f"Budżet na km sieci ({fmt_pln(budget['user_per_km'])}/km) jest niższy niż w {_pct(1 - budget['user_percentile'])} podobnych inwestycji.",
            f"koszt/km: mediana {fmt_pln(budget['per_km']['median'])}, N={budget['per_km']['n']}")
    ch = cost_fc.get("change_pct", {})
    if ch.get("n", 0) >= 5 and cost_fc.get("share_increase") and cost_fc["share_increase"] >= 0.35:
        add("amber", f"W {_pct(cost_fc['share_increase'])} podobnych umów wypłacono więcej niż pierwotna wartość umowy (90. percentyl: {_sg(ch['p90'])}).",
            f"N={ch['n']}")
    pve = tender.get("price_vs_estimate_pct", {})
    if pve.get("n", 0) >= 4 and pve.get("median") is not None and pve["median"] > 10:
        add("amber", f"Ceny ofert były zwykle wyższe od szacunku zamawiającego (mediana +{pve['median']:.0f}%) – ryzyko unieważnienia lub dofinansowania budżetu.",
            f"N={pve['n']}")
    c = tender.get("cancellations", {})
    if c.get("n", 0) >= 8 and c.get("share") and c["share"] >= 0.25:
        add("amber", f"{_pct(c['share'])} podobnych postępowań (lub ich części) unieważniono; {c.get('over_budget', 0)} z powodu ceny powyżej budżetu.",
            f"{c['k']} z {c['n']}")
    if P.get("design_build"):
        add("amber", "Formuła „zaprojektuj i wybuduj”: ryzyko niepełnego PFU (R01, R09) i decyzji uzyskiwanych przez wykonawcę (R04).",
            "katalog ekspercki MFiPR", "WNIOSEK")
    feats = set(P.get("features") or [])
    if "droga_kraj_woj" in feats or "kolej" in feats or "ciek" in feats:
        add("amber", "Przejścia przez drogi wyższej kategorii / kolej / cieki – uzgodnienia z podmiotami trzecimi (R04) i technologia bezwykopowa.",
            "elementy w opisie", "WNIOSEK")
    share = {r["code"]: r for r in risks}
    for feat, code, txt in (("tereny_prywatne", "R03", "Trasa przez grunty prywatne – brak zgód/służebności to częsta przyczyna zmian trasy i opóźnień"),
                            ("natura2000", "R04", "Obszar Natura 2000 / chroniony – wymagane rozpoznanie środowiskowe i dodatkowe uzgodnienia"),
                            ("wody_gruntowe", "R02", "Wysokie wody gruntowe / słabe grunty – ryzyko odwodnień, wymiany gruntu i robót dodatkowych"),
                            ("dofinansowanie_ue", "R13", "Finansowanie zewnętrzne – terminy rozliczenia dotacji wymuszają realny harmonogram")):
        if feat in feats:
            r = share.get(code)
            basis = f"{code}: {r['cases']} z {n} podobnych inwestycji ({_pct(r['share'])})" if r else f"{code} – katalog ekspercki MFiPR"
            add("amber", txt + ".", basis, "WNIOSEK")
    missing = [x for x in checklist if not x["present"]]
    if len(missing) >= 5:
        add("amber", f"Opis nie zawiera {len(missing)} z {len(checklist)} informacji z listy kontrolnej przygotowania – patrz „Do uzupełnienia”.",
            "lista kontrolna MFiPR", "WNIOSEK")
    for r in risks[:3]:
        if r["share"] >= 0.3 and r["cases"] >= 3:
            add("amber", f"Częsty problem w podobnych inwestycjach: {r['name']} ({r['cases']} z {n}).", f"{r['code']}, {r['events']} zdarzeń z dowodami")
    order = {"red": 0, "amber": 1, "info": 2}
    flags.sort(key=lambda f: order.get(f["level"], 3))
    return flags


def _recommendations(risks, checklist, P) -> dict[str, list[dict[str, Any]]]:
    before, actions, clauses = [], [], []
    for x in checklist:
        if not x["present"]:
            before.append({"text": x["text"], "ref": x.get("ref"), "why": "brak tej informacji w opisie inwestycji", "kind": "WNIOSEK"})
    seen_a, seen_c = set(), set()
    for r in risks[:6]:
        basis = f"{r['code']} {r['name']}: {r['cases']} podobnych przypadków ({_pct(r['share'])})"
        for m in r["mitigations"]:
            if m not in seen_a:
                seen_a.add(m)
                actions.append({"text": m, "risk": r["code"], "basis": basis, "kind": "WNIOSEK",
                                "evidence": [e["source_ref"] for e in r["examples"][:3]]})
        for c in r["clauses"]:
            if c not in seen_c:
                seen_c.add(c)
                clauses.append({"text": c, "risk": r["code"], "basis": basis, "kind": "WNIOSEK",
                                "evidence": [e["source_ref"] for e in r["examples"][:3]]})
    return {"before_tender": before, "actions": actions, "clauses": clauses}


def _contacts(conn: sqlite3.Connection, group: list[dict]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for r in group:
        p = r["proc"]
        key = p.get("org_key")
        if not key or key in seen:
            continue
        seen.add(key)
        o = conn.execute("SELECT * FROM organizations WHERE org_key=?", (key,)).fetchone()
        o = dict(o) if o else {}
        problems = unj(p.get("risk_codes"), []) or []
        success = p.get("delayed") == 0 and (p.get("cost_change_pct") or 0) <= 2 and not p.get("improper")
        why = ("zrealizowali podobną inwestycję w terminie i bez wzrostu kosztów – mogą podzielić się dobrymi praktykami"
               if success else "napotkali problemy w podobnej inwestycji – wiedzą, czego unikać")
        subject = f"Prośba o konsultację – doświadczenia z inwestycji „{(p.get('title') or '')[:80]}”"
        body = (f"Dzień dobry,\n\nprzygotowujemy podobną inwestycję i znaleźliśmy w Biuletynie Zamówień Publicznych Państwa zadanie "
                f"„{p.get('title')}” (postępowanie {p.get('tender_id')}).\nCzy moglibyśmy porozmawiać o Państwa doświadczeniach "
                f"(harmonogram, koszty, uzgodnienia, współpraca z wykonawcą)?\n\nZ poważaniem")
        mail = o.get("email") or ""
        out.append({
            "org": p.get("org_name"), "nip": p.get("nip"), "city": o.get("city") or p.get("city"),
            "province": PROVINCES.get(p.get("province") or "", ("",))[0], "email": mail, "phone": o.get("phone"), "www": o.get("www"),
            "case_title": p.get("title"), "tender_id": p.get("tender_id"), "year": p.get("year"), "success": success, "why": why,
            "problems": problems, "similarity_pct": r.get("similarity_pct"),
            "mailto": f"mailto:{mail}?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(body)}" if mail else None,
            "source": "dane kontaktowe z ogłoszeń BZP (sekcja I – zamawiający)",
        })
        if len(out) >= 8:
            break
    return out


def _case_card(conn: sqlite3.Connection, r: dict, light: bool = False) -> dict[str, Any]:
    p = r["proc"]
    card = {
        "tender_id": p["tender_id"], "title": p.get("title"), "org": p.get("org_name"), "nip": p.get("nip"),
        "province": PROVINCES.get(p.get("province") or "", ("",))[0], "year": p.get("year"), "subsector": p.get("subsector"),
        "value": candidate_value(p), "contract_value": p.get("contract_value"), "paid_value": p.get("paid_value"),
        "cost_change_pct": p.get("cost_change_pct"), "delayed": p.get("delayed"), "delay_days": p.get("delay_days"),
        "planned_days": p.get("planned_days"), "actual_days": p.get("actual_days"), "n_changes": p.get("n_changes"),
        "length_km": p.get("length_km"), "design_build": p.get("design_build"), "status": p.get("status"),
        "risk_codes": [c for c in (unj(p.get("risk_codes"), []) or []) if c != "BRAK"], "exemplary": p.get("exemplary"),
        "similarity_pct": r.get("similarity_pct"), "score": r["score"], "why": r["why"], "components": r["components"],
        "url": f"https://ezamowienia.gov.pl/mp-client/search/list/{p['tender_id']}" if p["tender_id"].startswith("ocds") else None,
    }
    if not light:
        card["notices"] = rows(conn, "SELECT notice_number, notice_type, pub_date, url FROM notices WHERE tender_id=? ORDER BY pub_date", (p["tender_id"],))
    return card


def plain_summary(rep: dict[str, Any]) -> list[str]:
    """Deterministyczne podsumowanie prostym językiem (bez modelu – zawsze dostępne)."""
    s = []
    n = rep["sample"]["n"]
    t, c, b = rep["forecast"]["time"], rep["forecast"]["cost"], rep["forecast"]["budget"]
    if n == 0:
        return ["W bazie nie znaleziono podobnych zakończonych inwestycji. Skontaktuj się z ekspertem lub poszerz opis."]
    s.append(f"Znaleziono {n} podobnych, zakończonych inwestycji, z którymi porównano Twój projekt.")
    if t.get("delay_probability") is not None:
        s.append(f"W {_pct(t['delay_probability'])} z nich umowa skończyła się po pierwotnym terminie "
                 f"(przedział 90%: {_pct(t['delay_ci90'][0])}–{_pct(t['delay_ci90'][1])}).")
    if t.get("actual_days", {}).get("n"):
        s.append(f"Typowy rzeczywisty czas robót: {_days_txt(t['actual_days']['median'])} (połowa realizacji trwała dłużej).")
    ch = c.get("change_pct", {})
    if ch.get("n"):
        s.append(f"Koszt: w {_pct(c['share_increase'])} umów wypłacono więcej niż w umowie; mediana zmiany {_sg(ch['median'])}, "
                 f"w 10% najgorszych przypadków ponad {_sg(ch['p90'])}.")
    if b.get("verdict"):
        s.append({"niski": "Planowany budżet na kilometr sieci jest NISKI na tle podobnych inwestycji – sprawdź kosztorys.",
                  "wysoki": "Planowany budżet na kilometr sieci jest wysoki na tle podobnych inwestycji (bezpieczny zapas lub zawyżony kosztorys).",
                  "typowy": "Planowany budżet na kilometr sieci mieści się w typowym zakresie podobnych inwestycji."}[b["verdict"]])
    if rep["risks"]:
        top = ", ".join(f"{r['name'].lower()} ({_pct(r['share'])})" for r in rep["risks"][:3])
        s.append(f"Najczęstsze problemy w podobnych inwestycjach: {top}.")
    if rep["sample"]["low_data"]:
        s.append("Uwaga: danych jest mało – skontaktuj się z zamawiającymi z listy kontaktów, aby potwierdzić wnioski.")
    return s
