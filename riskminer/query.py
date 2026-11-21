"""Zrozumienie opisu nowej inwestycji (język naturalny / wstępne dokumenty) -> „miękkie zapytanie”.

Domyślnie reguły (działa offline), opcjonalnie wzbogacone modelem językowym. Każdy parametr ma cytat
z tekstu i źródło ('reguła' / 'model' / 'użytkownik'), a użytkownik może go poprawić przed wyszukiwaniem."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import llm
from .classify import Classifier, extract_budget, extract_connections, extract_duration_days, extract_length_km
from .textutil import fold, stems

PROVINCES = {
    "PL02": ("dolnośląskie", "dolnoslask"), "PL04": ("kujawsko-pomorskie", "kujawsko"), "PL06": ("lubelskie", "lubelsk"),
    "PL08": ("lubuskie", "lubusk"), "PL10": ("łódzkie", "lodzk"), "PL12": ("małopolskie", "malopolsk"),
    "PL14": ("mazowieckie", "mazowieck"), "PL16": ("opolskie", "opolsk"), "PL18": ("podkarpackie", "podkarpack"),
    "PL20": ("podlaskie", "podlask"), "PL22": ("pomorskie", "pomorsk"), "PL24": ("śląskie", "slask"),
    "PL26": ("świętokrzyskie", "swietokrzysk"), "PL28": ("warmińsko-mazurskie", "warminsko"), "PL30": ("wielkopolskie", "wielkopolsk"),
    "PL32": ("zachodniopomorskie", "zachodniopomorsk"),
}
TERYT_TO_PL = {f"{int(k[2:]):02d}": k for k in PROVINCES}

RISK_HINTS = {
    "R03": r"kolizj|wlasciciel|grunt\w* prywatn|sluzebnos|uzbrojen",
    "R04": r"pozwoleni|decyzj|uzgodn|zarzadc\w* drog|wodnoprawn|natura 2000|srodowisk",
    "R02": r"wod\w* gruntow|geotechn|geolog|grunt\w* (nienosn|slab)|podmokl",
    "R13": r"dofinansow|budzet|wpf|kpo|feniks|srodk\w* (wlasn|finans)",
    "R06": r"termin|harmonogram|pilne|szybko",
    "R12": r"wykonawc",
    "R15": r"przylacz\w* (mieszkanc|nieruchomo)|liczb\w* odbiorcow|rlm",
}


def province_from_text(t: str) -> tuple[str | None, str]:
    f = fold(t)
    # „województwie X”, „woj. X”, a także sama nazwa przymiotnikowa
    for code, (name, stem_) in sorted(PROVINCES.items(), key=lambda kv: -len(kv[1][1])):
        # „pomorsk” nie może trafiać w „zachodniopomorskie” / „kujawsko-pomorskie”
        for m in re.finditer(stem_, f):
            before = f[max(0, m.start() - 12):m.start()]
            if stem_ == "pomorsk" and re.search(r"(zachodnio|kujawsko-|kujawsko )$", before):
                continue
            if stem_ == "slask" and before.endswith("dolno"):
                continue
            return code, t[max(0, m.start() - 15):m.end() + 10]
    return None, ""


def city_and_org(text: str) -> tuple[str, str, str]:
    """Zwraca (zamawiający, miejscowość, cytat)."""
    t = text.replace("\n", " ")
    org = ""
    m = re.search(r"\b((?:Gmina|Miasto i Gmina|Miasto|Gmina Miejska|Gmina Wiejska|Powiat|Związek Gmin|Przedsiębiorstwo Wodociągów i Kanalizacji|"
                  r"Zakład Gospodarki Komunalnej|Wodociągi)[^,.;:()\n]{2,60})", t)
    if m:
        org = re.split(r"\s+(planuje|przygotowuje|zamierza|realizuje|chce|ogłasza|prowadzi|ma|wraz|w ramach|jest)\b", m.group(1))[0].strip()
    city = ""
    mc = re.search(r"(?:w miejscowości|w miejscowościach|w m\.|we wsi|w mieście|w obrębie|sołectwo|sołectwie)\s+([A-ZŁŚŻŹĆÓ][\wąćęłńóśźż-]+(?:\s+[A-ZŁŚŻŹĆÓ][\wąćęłńóśźż-]+)?)", t)
    quote = ""
    if mc:
        city, quote = mc.group(1), mc.group(0)
    elif org:
        parts = org.split()
        city = parts[-1] if len(parts) > 1 else ""
        quote = org
    return org, city, quote


def _quote(text: str, rx: str, width: int = 60) -> str:
    m = re.search(rx, fold(text))
    if not m:
        return ""
    # fold() nie zmienia długości dla polskich znaków (poza ł->l, też 1:1), więc indeksy pasują
    return text[max(0, m.start() - width // 2):m.end() + width].replace("\n", " ").strip()


def understand(conn: sqlite3.Connection, text: str, use_llm: bool = True) -> dict[str, Any]:
    clf = Classifier(conn)
    text = text.strip()
    f = fold(text)
    first = text[:600]
    sub, extra = clf.classify_subsector(first, text)
    if not sub:
        sub, extra = clf.classify_subsector("", text)
    q: dict[str, Any] = {"text": text, "params": {}, "warnings": [], "method": "reguły"}
    P = q["params"]

    def put(key, value, quote="", source="reguła", label=None):
        P[key] = {"value": value, "quote": quote, "source": source}

    sub_names = {s["code"]: s["name"] for s in clf.subsectors}
    put("subsector", sub, _quote(text, r"kanalizac|wodociag|oczyszczal|uzdatnian|deszczow|retencj|ujeci"))
    put("subsectors_extra", extra)
    put("sector", "wod-kan" if sub else "inne")
    wt = clf.works_type(first) or clf.works_type(text)
    put("works_type", wt, _quote(text, r"\b(budow|rozbudow|przebudow|moderniz|remont|renowac)\w*"))
    db_ = clf.design_build(text)
    put("design_build", db_, _quote(text, r"zaprojektuj|projektuj i buduj|\bpfu\b|zaprojektowani") if db_ else "")
    b = extract_budget(text)
    put("budget", b["value"] if b else None, b["quote"] if b else "")
    put("budget_vat", b["vat"] if b else "nieokreślone")
    d = extract_duration_days(text)
    put("duration_days", d["days"] if d else None, d["quote"] if d else "")
    L = extract_length_km(text)
    put("length_km", L, _quote(text, r"\d+[,.]?\d*\s*(km|kilometr|mb\b|m\b)") if L else "")
    con = extract_connections(text)
    put("connections", con, _quote(text, r"\d+\s*(szt\.?\s*)?przylacz|przylacz\w*[^0-9]{0,20}\d+") if con else "")
    feats = clf.features_of(text)
    put("features", feats)
    prov, pq = province_from_text(text)
    org, city, cq = city_and_org(text)
    if not prov and org:
        # województwo z bazy organizacji (np. „Gmina Rydzyna” -> wielkopolskie)
        r = conn.execute("SELECT province FROM organizations WHERE name LIKE ? AND province LIKE 'PL%' LIMIT 1", (org + "%",)).fetchone()
        if r:
            prov, pq = r[0], f"(ustalono z bazy zamawiających: {org})"
    put("province", prov, pq)
    put("org", org, cq)
    put("city", city, cq)
    hints = [code for code, rx in RISK_HINTS.items() if re.search(rx, f)]
    put("risk_hints", hints)
    put("keywords", list(dict.fromkeys(stems(first)))[:25])

    if use_llm and llm.enabled():
        try:
            r = llm.understand_query(text)
            q["llm"] = r
            q["method"] = "reguły + model językowy"
            src = "model"
            if r.get("podsektor") in sub_names and not sub:
                put("subsector", r["podsektor"], "", src)
            if r.get("kwota_pln") and not P["budget"]["value"]:
                put("budget", float(r["kwota_pln"]), "", src)
            if r.get("czas_miesiace") and not P["duration_days"]["value"]:
                put("duration_days", int(float(r["czas_miesiace"]) * 30.4375), "", src)
            if r.get("dlugosc_km") and not P["length_km"]["value"]:
                put("length_km", float(r["dlugosc_km"]), "", src)
            if r.get("liczba_przylaczy") and not P["connections"]["value"]:
                put("connections", int(r["liczba_przylaczy"]), "", src)
            if r.get("streszczenie"):
                q["summary_llm"] = r["streszczenie"]
        except Exception as e:  # model niedostępny – zostają reguły
            q["warnings"].append(f"Model językowy niedostępny ({type(e).__name__}) – użyto reguł.")
    if not sub:
        q["warnings"].append("Nie rozpoznano podsektora wod-kan – wyszukiwanie oprze się głównie na treści opisu.")
    q["labels"] = {"subsector": sub_names.get(P["subsector"]["value"], ""), "province": PROVINCES.get(prov or "", ("", ""))[0],
                   "features": [clf.feature_names().get(x, x) for x in feats]}
    return q


def apply_overrides(q: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Poprawki użytkownika (formularz parametrów) nadpisują wartości z reguł/modelu."""
    for k, v in (overrides or {}).items():
        if k in q["params"]:
            if q["params"][k]["value"] != v:
                q["params"][k] = {"value": v, "quote": "", "source": "użytkownik"}
        else:
            q["params"][k] = {"value": v, "quote": "", "source": "użytkownik"}
    return q
