"""Wyszukiwanie podobnych inwestycji – hybryda: treść (BM25/FTS5 z polskim stemmingiem) + cechy strukturalne
(podsektor, skala kosztu i długości, rodzaj robót, formuła, elementy techniczne, region, aktualność).

Wagi są ustawieniem eksperckim (settings: search.weights) – zmiana bez kodu. Każdy wynik zawiera
rozbicie punktacji i zdania wyjaśniające, które cechy przesądziły o podobieństwie."""

from __future__ import annotations

import datetime as dt
import math
import re
import sqlite3
from typing import Any

from . import catalog
from .db import setting, unj
from .query import PROVINCES
from .textutil import STOPWORDS, fmt_pln, stem, tokens

GENERIC_STEMS = {"budow", "siec", "sieci", "wraz", "robot", "zadani", "realiz", "wykona", "gminy", "gmina", "gminie", "miejsc", "zakres",
                 "inwest", "projek", "ul", "dz", "nr", "obreb", "terenie", "teren", "przedm", "zamowi", "ramach", "etap", "czesc",
                 "dotycz", "wykon", "polega", "obejmu", "planuj", "planow"}

# słowa potoczne z opisów pisanych przez urzędnika – nie niosą treści technicznej (np. „w naszej gminie” ≠ „ulica Naszej”)
QUERY_STOP = set("""nasz nasza nasze naszej naszego naszym naszych nasza naszą chcemy chce chcę chcielibysmy chcielibyśmy planujemy planuje
zamierzamy potrzebujemy musimy bedziemy będziemy mamy mam prosze proszę pomoc pomocy jakas jakaś jakis jakiś jakies jakieś jakiej
okolo około mniej wiecej więcej przyszlym przyszłym przyszly przyszły nastepnym następnym tego takze także bardzo dobrze moze może
ktos ktoś cos coś czyli wlasnie właśnie""".split())

WORKS_GROUPS = [{"budowa", "rozbudowa"}, {"przebudowa", "modernizacja", "remont/renowacja"}]
VAT = 1.23


def _fts_query(text: str, max_terms: int = 40) -> str:
    seen, terms = set(), []
    for t in tokens(text):
        s = stem(t)
        if len(s) < 3 or s in seen or s in GENERIC_STEMS or s.isdigit() or t in STOPWORDS or t in QUERY_STOP:
            continue
        seen.add(s)
        terms.append(s)
        if len(terms) >= max_terms:
            break
    return " OR ".join(f'"{s}"*' for s in terms)


def _log_sim(a: float | None, b: float | None, k: float = 2.0) -> float | None:
    if not a or not b or a <= 0 or b <= 0:
        return None
    return math.exp(-abs(math.log10(a) - math.log10(b)) * k)


def _gross(value: float | None, vat: str) -> float | None:
    if value is None:
        return None
    return value * VAT if vat == "netto" else value


def candidate_value(p: dict) -> float | None:
    """Wartość brutto przypadku: umowa (brutto) > szacunek (netto*1,23)."""
    if p.get("contract_value"):
        return p["contract_value"]
    if p.get("estimated_value_net"):
        return p["estimated_value_net"] * VAT
    return None


def search(conn: sqlite3.Connection, q: dict[str, Any], top_k: int | None = None, only_completed: bool = True,
           exclude_tenders: set[str] | None = None, sector: str | None = None) -> list[dict[str, Any]]:
    P = {k: v.get("value") for k, v in q["params"].items()}
    W = setting(conn, "search.weights", catalog.DEFAULT_SETTINGS["search.weights"])
    top_k = top_k or setting(conn, "search.top_k", 25)
    boost = setting(conn, "report.exemplary_boost", 0.05)
    text = q.get("text", "")
    # do zapytania tekstowego: pierwsze ~2500 znaków + elementy techniczne
    # słowa kluczowe od modelu językowego (jeśli był użyty) idą na początek – nie zostaną obcięte limitem terminów
    ftsq = _fts_query(" ".join(q.get("search_terms") or []) + " " + text[:2500] + " " + " ".join(q.get("labels", {}).get("features", [])))
    cands: dict[str, float] = {}
    if ftsq:
        try:
            for tid, bm in conn.execute(
                    "SELECT tender_id, bm25(procedures_fts, 0.0, 4.0, 1.0, 0.4) AS s FROM procedures_fts WHERE procedures_fts MATCH ? ORDER BY s LIMIT 2500",
                    (ftsq,)):
                cands[tid] = -bm
        except sqlite3.OperationalError:
            pass
    # kandydaci strukturalni: ten sam podsektor i zbliżona skala (żeby nie zależeć tylko od słów)
    sub = P.get("subsector")
    budget = _gross(P.get("budget"), P.get("budget_vat") or "nieokreślone")
    if sub:
        sql = "SELECT tender_id FROM procedures WHERE subsector=?" + (" AND has_outcome=1" if only_completed else "")
        args: list = [sub]
        if budget:
            sql += " AND contract_value BETWEEN ? AND ?"
            args += [budget / 4, budget * 4]
        sql += " ORDER BY year DESC LIMIT 1500"
        for (tid,) in conn.execute(sql, args):
            cands.setdefault(tid, 0.0)
    if not cands:
        return []
    ids = list(cands)
    procs: dict[str, dict] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        cond = "tender_id IN (%s)" % ",".join("?" * len(chunk))
        if only_completed:
            cond += " AND has_outcome=1"
        if sector:
            cond += " AND sector=?"
        for r in conn.execute(f"SELECT * FROM procedures WHERE {cond}", chunk + ([sector] if sector else [])):
            procs[r["tender_id"]] = dict(r)
    if exclude_tenders:
        for t in exclude_tenders:
            procs.pop(t, None)
    if not procs:
        return []
    max_bm = max((cands[t] for t in procs), default=1.0) or 1.0
    qfeats = set(P.get("features") or [])
    qextra = set(P.get("subsectors_extra") or [])
    this_year = dt.date.today().year
    qkw = set(s for s in (stem(t) for t in tokens(text[:800])) if s not in GENERIC_STEMS and len(s) >= 4)
    sub_names = {s["code"]: s["name"] for s in catalog.SUBSECTORS}
    feat_names = {f["code"]: f["name"] for f in catalog.FEATURES}

    results = []
    for tid, p in procs.items():
        comp: dict[str, float] = {}
        why: list[str] = []
        # treść
        t_sim = (cands.get(tid, 0.0) / max_bm) if max_bm > 0 else 0.0
        comp["text"] = max(0.0, min(1.0, t_sim)) ** 0.7
        title_kw = set(stem(t) for t in tokens(p.get("title") or "")) & qkw
        if title_kw:
            why.append("wspólne słowa w nazwie/opisie: " + ", ".join(sorted(_unstem(title_kw, p.get("title") or ""))[:6]))
        # podsektor
        psub = p.get("subsector") or ""
        pextra = set(unj(p.get("subsectors_extra"), []) or [])
        if sub and psub == sub:
            comp["subsector"] = 1.0
            why.append(f"ten sam podsektor: {sub_names.get(sub, sub)}")
        elif sub and (psub in qextra or sub in pextra):
            comp["subsector"] = 0.6
            why.append(f"pokrewny zakres: {sub_names.get(psub, psub)}")
        else:
            comp["subsector"] = 0.0
        # skala: wartość i długość sieci
        pv = candidate_value(p)
        s_val = _log_sim(budget, pv)
        s_len = _log_sim(P.get("length_km"), p.get("length_km"), 1.5)
        sims = [x for x in (s_val, s_len) if x is not None]
        comp["scale"] = sum(sims) / len(sims) if sims else 0.3
        if s_val is not None and s_val >= 0.5:
            why.append(f"podobna skala kosztu: {fmt_pln(pv)} (planowane: {fmt_pln(budget)})")
        if s_len is not None and s_len >= 0.5:
            why.append(f"podobna długość sieci: {p['length_km']:.1f} km (planowane: {P['length_km']:.1f} km)".replace(".", ","))
        # rodzaj robót
        qw, pw = P.get("works_type") or "", p.get("works_type") or ""
        if qw and pw == qw:
            comp["works_type"] = 1.0
            why.append(f"ten sam rodzaj robót: {qw}")
        elif qw and any(qw in g and pw in g for g in WORKS_GROUPS):
            comp["works_type"] = 0.6
        else:
            comp["works_type"] = 0.0
        # formuła „zaprojektuj i wybuduj”
        qdb = bool(P.get("design_build"))
        comp["formula"] = 1.0 if qdb == bool(p.get("design_build")) else 0.0
        if qdb and p.get("design_build"):
            why.append("ta sama formuła: „zaprojektuj i wybuduj”")
        # elementy techniczne
        pf = set(unj(p.get("features"), []) or [])
        common = qfeats & pf
        union = qfeats | pf
        comp["features"] = (len(common) / len(union)) if union else 0.0
        if common:
            why.append("wspólne elementy: " + ", ".join(feat_names.get(c, c) for c in sorted(common)))
        # region
        prov = P.get("province")
        comp["region"] = 1.0 if prov and p.get("province") == prov else 0.0
        if comp["region"]:
            why.append(f"to samo województwo ({PROVINCES.get(prov, (prov,))[0]})")
        # aktualność (ceny i przepisy) – ostatnie 3 lata pełna waga
        yr = p.get("year") or this_year - 5
        comp["recency"] = max(0.0, 1.0 - max(0, this_year - yr - 2) * 0.2)
        score = sum(W.get(k, 0) * v for k, v in comp.items())
        if p.get("exemplary"):
            score += boost
            why.append("przypadek oznaczony przez eksperta jako wzorcowy")
        results.append({"tender_id": tid, "score": round(score, 4), "components": {k: round(v, 3) for k, v in comp.items()},
                        "why": why, "proc": p})
    results.sort(key=lambda r: -r["score"])
    top = results[:top_k]
    if top:
        mx = top[0]["score"] or 1.0
        for r in top:
            r["similarity_pct"] = int(round(100 * r["score"] / max(mx, 1e-9) * min(1.0, mx / 0.75)))
    return top


def _unstem(stems_: set[str], title: str) -> list[str]:
    out = []
    for t in tokens(title):
        if stem(t) in stems_ and t not in out:
            out.append(t)
    return out
