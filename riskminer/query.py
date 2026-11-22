"""Zrozumienie opisu nowej inwestycji (język naturalny / wstępne dokumenty) -> „miękkie zapytanie”.

Domyślnie reguły (działa offline), opcjonalnie wzbogacone modelem językowym. Każdy parametr ma cytat
z tekstu i źródło ('reguła' / 'model' / 'użytkownik'), a użytkownik może go poprawić przed wyszukiwaniem."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import llm
from .classify import Classifier, extract_budget, extract_connections, extract_duration_days, extract_length_km
from .textutil import fmt_pln, fold, parse_amount, stems

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
    sector, squote = sector_from_text(text, sub)
    if sector != "wod-kan" and sub:   # np. droga z kanalizacją deszczową: kanalizacja jako zakres pokrewny
        extra, sub = list(dict.fromkeys([sub] + list(extra))), ""
    put("subsector", sub, _quote(text, r"kanalizac|wodociag|oczyszczal|uzdatnian|deszczow|retencj|ujeci") if sub else "")
    put("subsectors_extra", extra)
    put("sector", sector, squote)
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
    if sector in ("drogi", "kolej"):
        # drogi i tory: „na odcinku 3,2 km” to długość całej inwestycji (w sieciach wod-kan bywa fragmentem)
        m = re.search(r"(\d+(?:[,.]\d+)?)\s*(km|kilometr)", f)
        if m and not L:
            L = float(m.group(1).replace(",", "."))
    if L is not None and sector in ("drogi", "kolej", "kubatura", "energetyka") and L < 0.2:
        L = None   # np. most o długości 42 m – to nie długość trasy
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
        _merge_llm(clf, q, text, put)
    Lm = P["length_km"]["value"]
    if Lm is not None and P["sector"]["value"] in ("drogi", "kolej", "kubatura", "energetyka") and Lm < 0.2:
        put("length_km", None)   # długość obiektu (np. mostu 42 m), a nie trasy – nie do porównań kosztu na km
    sub = P["subsector"]["value"]
    q["vague"] = _is_vague(P) or (q.get("llm", {}).get("czy_konkretna_inwestycja") is False and not sub and P["sector"]["value"] == "inne")
    if q["vague"]:
        q["warnings"].insert(0, "Opis jest zbyt ogólny: nie wiadomo, co ma zostać zbudowane. Dopisz rodzaj inwestycji (np. kanalizacja, "
                                "droga, budynek szkoły, fotowoltaika), skalę (długość, powierzchnia, moc), budżet i termin – inaczej porównanie będzie mało wiarygodne.")
    if not sub and P["sector"]["value"] == "inne":
        q["warnings"].append("Nie rozpoznano rodzaju inwestycji – wyszukiwanie oprze się głównie na treści opisu.")
    q["labels"] = {"subsector": sub_names.get(P["subsector"]["value"], ""), "sector": SECTOR_NAMES.get(P["sector"]["value"], ""), "province": PROVINCES.get(P["province"]["value"] or "", ("", ""))[0],
                   "features": [clf.feature_names().get(x, x) for x in P["features"]["value"]]}
    return q


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", fold(s or "")).strip(" .,;:\"'„”")


def verify_quote(quote: str, text: str) -> bool:
    """Czy cytat podany przez model naprawdę występuje w opisie (dokładnie albo ≥75% słów)."""
    q = _norm(quote)
    if len(q) < 2:
        return False
    t = _norm(text)
    if q in t:
        return True
    words = [w for w in re.findall(r"\w+", q) if len(w) > 2]
    tw = set(re.findall(r"\w+", t))
    return bool(words) and sum(w in tw for w in words) / len(words) >= 0.75


WORD_NUM = {"pol": 0.5, "poltora": 1.5, "jeden": 1, "jedna": 1, "jednego": 1, "dwa": 2, "dwie": 2, "dwoch": 2, "trzy": 3, "trzech": 3,
            "cztery": 4, "piec": 5, "szesc": 6, "osiem": 8, "dziewiec": 9, "dziesiec": 10, "dwanascie": 12, "pietnascie": 15,
            "osiemnascie": 18, "dwadziescia": 20, "dwadziescia cztery": 24, "trzydziesci": 30, "trzydziesci szesc": 36}


def _num(s: str) -> float | None:
    s = fold(str(s or "")).strip()
    if s in WORD_NUM:
        return float(WORD_NUM[s])
    m = re.search(r"\d+(?:[.,]\d+)?", s.replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _is_vague(P: dict) -> bool:
    v = lambda k: P.get(k, {}).get("value")
    known = v("subsector") or (v("sector") or "inne") != "inne"
    signals = sum(bool(x) for x in (known, v("works_type"), v("budget"), v("length_km"), v("connections"), v("duration_days")))
    return not known and signals <= 1 and len(v("features") or []) <= 1


SECTOR_NAMES = {"wod-kan": "Gospodarka wodno-ściekowa", "drogi": "Drogi i mosty", "kubatura": "Budynki i obiekty kubaturowe",
                "energetyka": "Energetyka i OZE", "kolej": "Kolej i transport szynowy", "inne": "Inne"}


def sector_from_text(text: str, sub: str = "") -> tuple[str, str]:
    """Sektor główny = rodzaj obiektu wymieniony najwcześniej w początku opisu (tytuł / pierwsze zdania)."""
    from .catalog import SECTOR_PATTERNS, SUBSECTORS
    f = fold(text)
    pats = {k: v for k, v in SECTOR_PATTERNS.items()}
    pats["wod-kan"] = [p for s_ in SUBSECTORS for p in s_.get("patterns", [])] + [r"kanalizac", r"wodociag"]
    best = None
    for window in (f[:400], f):
        for sec, rx in pats.items():
            for p in rx:
                m = re.search(p, window)
                if m and (best is None or m.start() < best[1]):
                    best = (sec, m.start(), m.end())
        if best:
            break
    if not best:
        return ("wod-kan" if sub else "inne"), ""
    return best[0], text[max(0, best[1] - 30):best[2] + 40].replace("\n", " ").strip()


# elementy zakresu rzeczowego (ekstrakcja) liczone do długości sieci i do liczby przyłączy
_NETWORK = {"kanalizacja sanitarna grawitacyjna", "kanalizacja sanitarna tłoczna", "kanalizacja sanitarna (inna/nieokreślona)",
            "kanalizacja deszczowa", "sieć wodociągowa"}
_CONNECTIONS = {"przyłącza kanalizacyjne", "przyłącza wodociągowe"}


def _merge_llm(clf: Classifier, q: dict, text: str, put) -> None:
    """Model językowy wypełnia formularz parametrów – ta sama instrukcja, schemat i weryfikacja co w ekstraktorze
    (riskminer.ekstrakcja): cytaty sprawdzane w tekście, kwota nie może być zamawiającym, daty spoza tekstu odrzucane,
    czas od daty podpisania umowy, zakres rzeczowy w liczbach. Kategorie (zamawiający, miejsce) – pierwszeństwo ma model;
    liczby – pierwszeństwo mają reguły, model uzupełnia braki. Rozbieżności są pokazywane użytkownikowi."""
    import datetime as dt
    import time
    from . import ekstrakcja as ek
    P = q["params"]
    t0 = time.time()
    try:
        llm.reset_last()
        raw = llm.extract_params(text)
    except Exception as e:  # model niedostępny – zostają reguły
        q["warnings"].append(f"Model językowy niedostępny ({type(e).__name__}) – parametry odczytano regułami.")
        return
    r = ek.postprocess(raw if isinstance(raw, dict) else {}, text, dt.date.today())
    q["llm"] = raw
    q["ekstrakcja"] = r
    used = llm.last_used()
    q["llm_info"] = {"model": used.get("model"), "seconds": round(time.time() - t0, 1), "where": used.get("where"), "fallback": used.get("fallback")}
    if used.get("fallback"):
        q["warnings"].append(f"Główny model był niedostępny – parametry odczytał model zapasowy {used.get('model')} ({used.get('where')}).")
    q["method"] = "model językowy + reguły"
    conflicts = q.setdefault("conflicts", {})
    for key, label in (("zamawiajacy", "zamawiający"), ("miejscowosc", "miejscowość"), ("wojewodztwo", "województwo"),
                       ("planowana_kwota", "kwota"), ("planowany_czas", "czas realizacji")):
        rej = (r.get(key) or {}).get("odrzucone")
        if rej:
            q["warnings"].append(f"Odrzucono odpowiedź modelu ({label}): „{rej.get('wartosc')}” – {rej.get('powod')}.")
    if r["planowany_czas"].get("uwaga"):
        q["warnings"].append("Czas realizacji: " + r["planowany_czas"]["uwaga"])

    def category(key, f, value=None):
        """Wartość od modelu (po weryfikacji): przyjęta, gdy cytat jest w tekście albo reguły nic nie znalazły."""
        value = f.get("wartosc") if value is None else value
        quote = f.get("cytat") or ""
        ok = f.get("zrodlo") not in (None, "", "brak") and verify_quote(quote, text)
        rule = P[key]["value"]
        if value in (None, "") or (not ok and rule not in (None, "", False)):
            return
        if rule not in (None, "", False) and rule != value:
            conflicts[key] = f"reguły: {rule}"
        put(key, value, quote if ok else "", "reguła" if f.get("z_reguly") else ("model i reguły" if rule == value else "model"))

    org = r["zamawiajacy"]
    if org.get("wartosc") and len(org["wartosc"]) < 90:
        category("org", org)
    city = r["miejscowosc"]
    if city.get("wartosc") and len(city["wartosc"]) < 60:
        category("city", city)
    w = r["wojewodztwo"]
    code = w.get("kod")
    if code:
        rule = P["province"]["value"]
        ok = w.get("zrodlo") not in (None, "", "brak") and verify_quote(w.get("cytat") or "", text)
        if rule and rule != code:
            conflicts["province"] = f"reguły: {PROVINCES[rule][0]}"
        if not rule or ok:
            put("province", code, w["cytat"] if ok else "(wywnioskowane przez model)", "model" if rule != code else "model i reguły")

    # liczby: reguły mają pierwszeństwo, model uzupełnia braki; rozbieżność > 10% – ostrzeżenie
    def number(key, value, quote, fmt=lambda x: x):
        if value is None or value <= 0:
            return
        rule = P[key]["value"]
        if rule:
            if abs(rule - value) / max(rule, value) > 0.10:
                conflicts[key] = f"model: {fmt(value)}"
            else:
                P[key]["source"] = "model i reguły"
                if verify_quote(quote, text):   # krótki, dokładny cytat modelu zamiast szerokiego okna reguły
                    P[key]["quote"] = quote
            return
        if verify_quote(quote, text):
            put(key, value, quote, "model")

    k = r["planowana_kwota"]
    if k.get("kwota") and (k.get("waluta") or "PLN") == "PLN" and k.get("zrodlo") != "brak":
        number("budget", float(k["kwota"]), k.get("cytat", ""), fmt_pln)
        if P["budget"]["source"] == "model" and k.get("vat") in ("netto", "brutto"):
            put("budget_vat", k["vat"], k.get("cytat", ""), "model")
    t = r["planowany_czas"]
    if t.get("dni") and 7 <= t["dni"] <= 3650:
        number("duration_days", int(t["dni"]), t.get("cytat", ""), lambda x: f"{x} dni")

    # zakres rzeczowy: długość sieci (m -> km) i liczba przyłączy – tylko pozycje z liczbą w cytacie, cytat w tekście
    items = [x for x in r["zakres_rzeczowy"] if x.get("liczba_w_cytacie") and x.get("zrodlo") != "brak"]
    lin = _NETWORK | ({"nawierzchnie i drogi"} if P["sector"]["value"] in ("drogi", "kolej") else set())
    net = [x for x in items if x["element"] in lin and x["jednostka_bazowa"] == "m"]
    if net:
        km = round(sum(x["ilosc_bazowa"] for x in net) / 1000.0, 3)
        number("length_km", km if km < 500 else None, max(net, key=lambda x: x["ilosc_bazowa"])["cytat"], lambda x: f"{x:g} km")
    con = [x for x in items if x["element"] in _CONNECTIONS and x["jednostka_bazowa"] == "szt."]
    if con:
        n = int(sum(x["ilosc_bazowa"] for x in con))
        number("connections", n if n < 20000 else None, con[0]["cytat"])

    # elementy techniczne: suma reguł i elementów rozpoznanych w cytatach zakresu rzeczowego
    feats = list(P["features"]["value"] or [])
    added = [f for f in clf.features_of(" ".join(x["cytat"] for x in items)) if f not in feats] if items else []
    if added:
        put("features", feats + added, "", "model i reguły" if feats else "model")
    kws = [x["fraza"] for x in r["slowa_kluczowe"] if x.get("w_tekscie") and 2 < len(x["fraza"]) < 60][:8]
    if kws:
        q["search_terms"] = kws


def apply_overrides(q: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Poprawki użytkownika (formularz parametrów) nadpisują wartości z reguł/modelu."""
    for k, v in (overrides or {}).items():
        if k in q["params"]:
            if q["params"][k]["value"] != v:
                q["params"][k] = {"value": v, "quote": "", "source": "użytkownik"}
        else:
            q["params"][k] = {"value": v, "quote": "", "source": "użytkownik"}
    if overrides:
        sub = q["params"].get("subsector", {}).get("value")
        if sub:
            q["params"]["sector"] = {"value": "wod-kan", "quote": "", "source": "użytkownik"}
        elif "sector" not in overrides and q["params"].get("sector", {}).get("value") == "wod-kan":
            q["params"]["sector"] = {"value": "inne", "quote": "", "source": "użytkownik"}
        q.setdefault("labels", {})["sector"] = SECTOR_NAMES.get(q["params"]["sector"]["value"], "")
        from .catalog import SUBSECTORS
        q["labels"]["subsector"] = next((s_["name"] for s_ in SUBSECTORS if s_["code"] == sub), "")
        # opis ogólny, ale użytkownik uzupełnił formularz – ocena na podstawie poprawionych parametrów
        q["vague"] = bool(q.get("vague")) and _is_vague(q["params"])
    return q
