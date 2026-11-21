"""Klasyfikacja i ekstrakcja cech: podsektor, rodzaj robót, formuła, elementy techniczne, długość sieci,
liczba przyłączy oraz rozpoznawanie zdarzeń ryzyka (reguły z katalogu eksperckiego w bazie).

Wszystkie reguły są czytane z bazy (risk_catalog.patterns, subsectors.patterns, settings.features),
więc ekspert zmienia je w panelu bez zmian w kodzie."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import catalog
from .db import setting, unj
from .textutil import fold, parse_amount

_NUM = r"(\d{1,3}(?:[ \xa0]\d{3})+|\d+)(?:[.,](\d+))?"


def _num(m_int: str, m_frac: str | None) -> float:
    return float(re.sub(r"\s", "", m_int) + ("." + m_frac if m_frac else ""))


def extract_length_km(text: str) -> float | None:
    """Szacuje łączną długość sieci w km z opisu (np. '2,38 kilometra', '3794m', '11,871 km', '659,0 mb')."""
    t = fold(text)
    t = t.replace("\xa0", " ")
    tot_km = None
    tot = re.search(r"(?:lacznej|calkowit\w*|sumaryczn\w*) dlugosc\w*[^0-9]{0,40}" + _NUM + r"\s*(km|kilometr|m\b|mb\b|metr)", t)
    if tot:
        v = _num(tot.group(1), tot.group(2))
        tot_km = round(v if tot.group(3).startswith("k") else v / 1000.0, 3)
    vals = []
    for m in re.finditer(_NUM + r"\s*(km\b|kilometr\w*|mb\b|m\b|metr\w*|m\.b\.)", t):
        v = _num(m.group(1), m.group(2))
        unit = m.group(3)
        ctx = t[max(0, m.start() - 70):m.start()]
        # pomijamy głębokości, średnice, powierzchnie i odległości niezwiązane z siecią
        if re.search(r"glebok|szerok|wysok|srednic|dn\s*$|odleglos|powierzchn|m2|m3", ctx[-25:]):
            continue
        # fragmenty trasy już zawarte w długości całkowitej („część trasy (ok. 1,4 km) przebiega przez…”)
        if re.search(r"czesc\w* trasy|fragment\w*|w tym|z czego|na odcinku", ctx[-40:]):
            continue
        km = v if unit.startswith("k") else v / 1000.0
        if 0.01 <= km <= 300:
            vals.append(km)
    if not vals:
        return tot_km
    if tot_km and tot_km >= 0.9 * max(vals):
        return tot_km          # „o łącznej długości” obejmuje całość
    s, mx = sum(vals), max(vals)
    return round(s if s <= 2.2 * mx else mx, 3)


def extract_connections(text: str) -> int | None:
    t = fold(text)
    best = None
    for m in re.finditer(r"(\d{1,4})\s*(?:szt\.?\s*)?(?:przylacz|siegacz|przykanalik|odgalezien)", t):
        n = int(m.group(1))
        best = max(best or 0, n)
    for m in re.finditer(r"(?:przylacz\w*|siegacz\w*|przykanalik\w*)[^0-9.;]{0,25}?(\d{1,4})\s*(?:szt|przylacz|nieruchomo|posesj|budynk|\))", t):
        n = int(m.group(1))
        best = max(best or 0, n)
    return best if best and best < 5000 else None


def extract_budget(text: str) -> dict[str, Any] | None:
    """Największa kwota w opisie (mln/tys zł), z informacją netto/brutto."""
    t = text.replace("\xa0", " ")
    best = None
    for m in re.finditer(r"(\d[\d\s]*(?:[.,]\d+)?)\s*(mld|mln|milion\w*|tys\.?|tysi\w*)?\s*(zł|zl|pln|złotych)", t, re.I):
        v = parse_amount(m.group(1) + " " + (m.group(2) or ""))
        if v is None or v < 10000:
            continue
        ctx = fold(t[max(0, m.start() - 80):m.end() + 30])
        vat = "brutto" if "brutto" in ctx else "netto" if "netto" in ctx else "nieokreślone"
        if best is None or v > best["value"]:
            best = {"value": v, "vat": vat, "quote": t[max(0, m.start() - 60):m.end()].strip()}
    return best


def extract_duration_days(text: str) -> dict[str, Any] | None:
    t = fold(text)
    m = re.search(r"(\d{1,3})\s*(miesiec\w*|miesiac\w*|m-cy|mies\.|dni|tygodni\w*|lat\b|lata\b)", t)
    if m:
        n = int(m.group(1))
        u = m.group(2)
        mul = 30.4375 if u.startswith(("mies", "m-c")) else 7 if u.startswith("tyg") else 365.25 if u.startswith("lat") else 1
        days = int(round(n * mul))
        if 7 <= days <= 3650:
            return {"days": days, "quote": text[max(0, m.start() - 40):m.end() + 10]}
    return None


class Classifier:
    """Reguły klasyfikacji ładowane z bazy (edytowalne)."""

    def __init__(self, conn: sqlite3.Connection | None = None):
        if conn is not None:
            subs = [dict(r) for r in conn.execute("SELECT * FROM subsectors").fetchall()]
            self.subsectors = [{**s, "patterns": unj(s["patterns"], [])} for s in subs]
            risks = [dict(r) for r in conn.execute("SELECT * FROM risk_catalog WHERE status!='rejected'").fetchall()]
            self.risks = [{**r, "patterns": unj(r["patterns"], [])} for r in risks]
            self.features = setting(conn, "features", catalog.FEATURES)
        else:
            self.subsectors = catalog.SUBSECTORS
            self.risks = catalog.RISKS
            self.features = catalog.FEATURES
        self._sub_rx = [(s["code"], [re.compile(p) for p in s["patterns"]]) for s in self.subsectors if s.get("patterns")]
        self._risk_rx = []
        for r in self.risks:
            comp = []
            for p in r["patterns"]:
                try:
                    comp.append(re.compile(p))
                except re.error:
                    continue
            self._risk_rx.append((r["code"], r.get("status", "active"), comp))
        self._feat_rx = [(f["code"], f["name"], re.compile(f["pattern"])) for f in self.features]
        self._works_rx = [(name, re.compile(p)) for name, p in catalog.WORKS_TYPES]
        self._db_rx = re.compile(catalog.DESIGN_BUILD_RE)
        self._effect_rx = {k: re.compile(p) for k, p in catalog.EFFECT_PATTERNS.items()}
        self._action_rx = [(n, re.compile(p)) for n, p in catalog.ACTION_PATTERNS]
        self.risk_names = {r["code"]: r["name"] for r in self.risks}
        self._admin_rx = re.compile(catalog.ADMIN_CHANGE_RE)

    # ---------------------------------------------------------------- cechy inwestycji
    def subsector_scores(self, title: str, desc: str = "") -> dict[str, float]:
        ft, fd = fold(title), fold(desc)[:4000]
        scores: dict[str, float] = {}
        for code, rxs in self._sub_rx:
            s = 0.0
            for rx in rxs:
                s += 3.0 * len(rx.findall(ft)) + 1.0 * min(5, len(rx.findall(fd)))
            if s:
                scores[code] = s
        # "kanalizacja" ogólna nie powinna wygrywać z deszczową, gdy tytuł mówi o deszczowej
        if "deszcz" in scores and "kan_san" in scores and "sanitarn" not in ft and "deszczow" in ft:
            scores["kan_san"] *= 0.3
        return scores

    def classify_subsector(self, title: str, desc: str = "") -> tuple[str, list[str]]:
        sc = self.subsector_scores(title, desc)
        if not sc:
            return "", []
        order = sorted(sc.items(), key=lambda kv: -kv[1])
        main = order[0][0]
        extra = [c for c, v in order[1:] if v >= max(2.0, 0.34 * order[0][1])]
        return main, extra

    def works_type(self, text: str) -> str:
        t = fold(text)[:600]
        best, pos = "", 10 ** 9
        for name, rx in self._works_rx:
            m = rx.search(t)
            if m and m.start() < pos:
                best, pos = name, m.start()
        return best

    def design_build(self, text: str) -> bool:
        return bool(self._db_rx.search(fold(text)))

    def features_of(self, text: str) -> list[str]:
        t = fold(text)
        return [code for code, _, rx in self._feat_rx if rx.search(t)]

    def feature_names(self) -> dict[str, str]:
        return {code: name for code, name, _ in self._feat_rx}

    # ---------------------------------------------------------------- ryzyka
    def classify_risk(self, text: str, max_codes: int = 2, include_proposed: bool = True) -> list[dict[str, Any]]:
        """Zwraca listę dopasowanych kodów ryzyka z pewnością i dopasowanymi frazami."""
        t = fold(text)
        if not t.strip():
            return []
        out = []
        for code, status, rxs in self._risk_rx:
            if status == "proposed" and not include_proposed:
                continue
            hits = []
            for rx in rxs:
                m = rx.search(t)
                if m:
                    hits.append(m.group(0))
            if hits:
                conf = min(0.92, 0.45 + 0.17 * len(hits))
                if status == "proposed":
                    conf *= 0.85
                out.append({"code": code, "confidence": round(conf, 2), "terms": hits[:5], "status": status})
        # R06 (harmonogram) i R07 (roboty dodatkowe) opisują częściej SKUTEK niż przyczynę –
        # gdy rozpoznano też przyczynę, obniżamy ich pewność
        if any(o["code"] not in ("R06", "R07") for o in out):
            for o in out:
                if o["code"] in ("R06", "R07"):
                    o["confidence"] = round(o["confidence"] * 0.8, 2)
        out.sort(key=lambda x: -x["confidence"])
        return out[:max_codes]

    def is_administrative(self, text: str, codes: list[dict[str, Any]]) -> bool:
        """Zmiana administracyjna (rachunek, kierownik budowy, podwykonawca, VAT...) bez sygnałów ryzyka."""
        t = fold(text)
        if not self._admin_rx.search(t):
            return False
        strong = [c for c in codes if c["code"] not in ("R06", "R12", "R13", "R05") and c["confidence"] >= 0.6]
        return not strong

    def effect_types(self, text: str) -> list[str]:
        t = fold(text)
        return [k for k, rx in self._effect_rx.items() if rx.search(t)]

    def actions(self, text: str) -> list[str]:
        t = fold(text)
        return [n for n, rx in self._action_rx if rx.search(t)]


def time_extension_days(text: str) -> int | None:
    """'wydłużenie terminu o 2 miesiące' -> 61; 'przedłużenie terminu o 45 dni' -> 45."""
    t = fold(text)
    m = re.search(r"(?:wydluz|przedluz|zmian|przesun)\w*\s+(?:terminu|okresu|czasu)[^0-9]{0,60}?o\s*(\d{1,4})\s*(dni|dzien|tyg\w*|miesi\w*|m-c\w*)", t)
    if not m:
        m = re.search(r"o\s*(\d{1,4})\s*(dni|tyg\w*|miesi\w*)\s*(?:kalendarzow\w*\s*)?(?:dluzej|wydluz|przedluz)", t)
    if m:
        n = int(m.group(1))
        u = m.group(2)
        return int(round(n * (30.4375 if u.startswith(("miesi", "m-c")) else 7 if u.startswith("tyg") else 1)))
    return None


@dataclass
class ChangeAnalysis:
    codes: list[dict[str, Any]]
    effects: list[str]
    actions: list[str]
    time_ext: int | None
