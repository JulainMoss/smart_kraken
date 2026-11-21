"""Parser ogłoszeń BZP (e-Zamówienia, API mo-board/v1/notice).

Formularze BZP mają numerowane pola „1.5.9.) Adres poczty elektronicznej: ...”. Numeracja różni się
między wersjami formularzy, dlatego pola wyszukujemy po ETYKIECIE (wyrażenie regularne), a numer pola
zapisujemy tylko jako lokalizację dowodu (np. „sekcja 5.4.4”).

Obsługiwane typy: ContractNotice (ogłoszenie o zamówieniu), TenderResultNotice (wynik postępowania,
w tym wiele części), AgreementUpdateNotice (zmiana umowy), ContractPerformingNotice (wykonanie umowy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .textutil import digits, html_to_text, norm_bzp_number, norm_ws, parse_amount, parse_date, parse_period_days

FIELD_RE = re.compile(r"(?<![\d.\w])(\d{1,2}(?:\.\d{1,2}){1,4})\.?\)\s*")
PART_RE = re.compile(r"\(dla częś?ci\s+(\d+)\)", re.I)
FOOTER_RE = re.compile(r"\s*\d{4}-\d{2}-\d{2}\s+Biuletyn Zamówień Publicznych.*$", re.S)
TRAIL_RE = re.compile(r"\s*(SEKCJA [IVX]+\b.*|Część \d+\s*|Wykonawca\s*|Kryterium \d+\s*)$", re.S | re.I)

NOTICE_URL = "https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{object_id}"
TENDER_URL = "https://ezamowienia.gov.pl/mp-client/search/list/{tender_id}"


@dataclass
class Field:
    code: str
    label: str
    value: str
    part: int = 0


def parse_fields(text: str) -> list[Field]:
    """Dzieli tekst ogłoszenia na pola (kod, etykieta, wartość, część)."""
    marks = list(FIELD_RE.finditer(text))
    parts = [(m.start(), int(m.group(1))) for m in PART_RE.finditer(text)]
    out: list[Field] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        seg = text[m.end():end]
        seg = FOOTER_RE.sub("", seg)
        # etykieta do pierwszego dwukropka (o ile jest blisko), wartość - reszta
        mc = re.search(r":(?!//)", seg)
        c = mc.start() if mc else -1
        nl = seg.find("\n")
        if 0 <= c <= 400 and (nl < 0 or c <= nl + 200):
            label, value = seg[:c], seg[c + 1:]
        elif nl >= 0:
            label, value = seg[:nl], seg[nl + 1:]
        else:
            label, value = seg, ""
        value = TRAIL_RE.sub("", value.strip())
        # usunięcie nagłówków sekcji wewnątrz wartości (gdy sekcja zaczyna się po wartości)
        value = re.split(r"\n\s*SEKCJA [IVX]+", value)[0].strip()
        part = 0
        for pos, p in parts:
            if pos < m.start():
                part = p
        out.append(Field(m.group(1), norm_ws(label), value.strip(), part))
    return out


def find(fields: list[Field], label_re: str, part: int | None = None, default: str = "") -> str:
    rx = re.compile(label_re, re.I)
    for f in fields:
        if (part is None or f.part == part) and rx.search(f.label):
            return f.value
    return default


def find_field(fields: list[Field], label_re: str) -> Field | None:
    rx = re.compile(label_re, re.I)
    for f in fields:
        if rx.search(f.label):
            return f
    return None


def yes(v: str | None) -> bool | None:
    if not v:
        return None
    t = v.strip().lower()
    if t.startswith("tak"):
        return True
    if t.startswith("nie"):
        return False
    return None


def first_line(v: str, maxlen: int = 400) -> str:
    return norm_ws(v.split("\n")[0])[:maxlen]


def money(v: str | None) -> float | None:
    if not v:
        return None
    m = re.search(r"\d[\d\s]*(?:[.,]\d{1,2})?", v)
    return parse_amount(m.group(0)) if m else None


# ------------------------------------------------------------------------------------------
@dataclass
class ParsedNotice:
    notice_number: str
    notice_type: str
    tender_id: str
    object_id: str
    pub_date: str
    title: str
    org: dict[str, Any]
    data: dict[str, Any] = field(default_factory=dict)
    parts: list[dict[str, Any]] = field(default_factory=list)      # wynik: części / umowy
    changes: list[dict[str, Any]] = field(default_factory=list)    # zmiany umowy
    text: str = ""

    @property
    def url(self) -> str:
        return NOTICE_URL.format(object_id=self.object_id) if self.object_id else TENDER_URL.format(tender_id=self.tender_id)


def parse_org(rec: dict, fields: list[Field]) -> dict[str, Any]:
    nat = rec.get("organizationNationalId") or find(fields, r"Krajowy Numer Identyfikacyjny")
    email = find(fields, r"Adres poczty elektronicznej")
    www = find(fields, r"Adres strony internetowej zamawiającego")
    phone = find(fields, r"Numer telefonu")
    return {
        "name": norm_ws(rec.get("organizationName") or find(fields, r"Nazwa zamawiającego")),
        "nip": digits(nat) if len(digits(nat)) == 10 else "",
        "regon": (digits(find(fields, r"Krajowy Numer Identyfikacyjny")) if "REGON" in (find(fields, r"Krajowy Numer Identyfikacyjny") or "") else ""),
        "city": norm_ws(rec.get("organizationCity") or find(fields, r"^Miejscowość")),
        "province": rec.get("organizationProvince") or "",
        "street": first_line(find(fields, r"^Ulica")),
        "postcode": first_line(find(fields, r"Kod pocztowy")),
        "email": first_line(email, 120),
        "www": first_line(www, 200),
        "phone": first_line(phone, 60),
        "org_type": first_line(find(fields, r"Rodzaj zamawiającego"), 200),
        "nuts": first_line(find(fields, r"Lokalizacja NUTS"), 80),
    }


def parse_notice(rec: dict) -> ParsedNotice:
    text = html_to_text(rec.get("htmlBody") or "")
    fields = parse_fields(text)
    ntype = rec.get("noticeType") or ""
    pn = ParsedNotice(
        notice_number=norm_bzp_number(rec.get("noticeNumber")) or (rec.get("noticeNumber") or ""),
        notice_type=ntype,
        tender_id=rec.get("tenderId") or find(fields, r"Identyfikator postępowania"),
        object_id=rec.get("objectId") or "",
        pub_date=(rec.get("publicationDate") or "")[:10],
        title=norm_ws(rec.get("orderObject") or find(fields, r"Nazwa zamówienia")),
        org=parse_org(rec, fields),
        text=text,
    )
    d = pn.data
    d["cpv"] = rec.get("cpvCode") or ""
    d["order_type"] = rec.get("orderType") or ""
    d["below_eu"] = rec.get("isTenderAmountBelowEU")
    if ntype == "ContractNotice":
        _contract_notice(pn, fields)
    elif ntype == "TenderResultNotice":
        _result_notice(pn, fields, rec)
    elif ntype == "ContractPerformingNotice":
        _performing_notice(pn, fields)
    elif ntype == "AgreementUpdateNotice":
        _update_notice(pn, fields)
    return pn


def _contract_notice(pn: ParsedNotice, F: list[Field]) -> None:
    d = pn.data
    d["eu_funded"] = yes(find(F, r"współfinansowanego ze środków Unii"))
    d["eu_project"] = first_line(find(F, r"Nazwa projektu lub programu"), 300)
    d["tryb"] = first_line(find(F, r"Tryb udzielenia zamówienia"), 300)
    d["platform_url"] = first_line(find(F, r"Adres strony internetowej prowadzonego postępowania"), 300)
    d["ref_no"] = first_line(find(F, r"Numer referencyjny"), 80)
    d["estimated_value_net"] = money(find(F, r"^Wartość zamówienia"))
    d["parts_allowed"] = yes(find(F, r"Możliwe jest składanie ofert częściowych"))
    desc = find(F, r"Krótki opis przedmiotu zamówienia")
    d["description"] = desc[:6000]
    d["cpv_main"] = first_line(find(F, r"Główny kod CPV"), 200)
    d["period_txt"] = first_line(find(F, r"Okres realizacji"), 200)
    d["planned_days"] = parse_period_days(d["period_txt"], parse_date(pn.pub_date))
    d["changes_foreseen"] = yes(find(F, r"przewiduje zmiany umowy"))
    d["changes_clause"] = find(F, r"Rodzaj i zakres zmian umowy")[:3000]
    d["advance"] = yes(find(F, r"udzielenia zaliczek"))
    d["deadline"] = first_line(find(F, r"Termin składania ofert"), 40)
    # kryteria oceny – waga ceny
    crit = [(f.label, f.value) for f in F if re.search(r"Nazwa kryterium|Waga", f.label, re.I)]
    weights = []
    name = None
    for lab, val in crit:
        if "Nazwa kryterium" in lab:
            name = first_line(val, 80)
        elif "Waga" in lab and name:
            w = parse_amount(val)
            weights.append({"kryterium": name, "waga": w})
            name = None
    d["criteria"] = weights
    # wartości części
    parts_val = [money(f.value) for f in F if re.search(r"Wartość części", f.label, re.I)]
    if parts_val:
        d["parts_values"] = [p for p in parts_val if p]


def _result_notice(pn: ParsedNotice, F: list[Field], rec: dict) -> None:
    d = pn.data
    d["estimated_value_net"] = money(find(F, r"^Wartość zamówienia"))
    d["contract_notice"] = None
    for f in F:
        if re.match(r"Numer ogłoszenia", f.label) and f.code.startswith("2.1"):
            n = norm_bzp_number(f.value)
            if n and n != pn.notice_number:
                d["contract_notice"] = n
    d["tryb"] = first_line(find(F, r"Tryb udzielenia zamówienia"), 300)
    d["description"] = find(F, r"Krótki opis przedmiotu zamówienia")[:3000]
    part_ids = sorted({f.part for f in F if f.part > 0}) or [0]
    for p in part_ids:
        res = find(F, r"zakończyło się zawarciem umowy albo unieważnieniem", part=p)
        cancelled = bool(re.search(r"unieważni", res, re.I))
        part = {
            "part": p,
            "result": first_line(res, 200),
            "cancelled": cancelled,
            "cancel_basis": first_line(find(F, r"Podstawa prawna unieważnienia", part=p), 300),
            "cancel_reason": find(F, r"Uzasadnienie (unieważnienia|faktyczne)|Przyczyn[ay] unieważnienia", part=p)[:1500],
            "offers": _int(find(F, r"Liczba otrzymanych ofert", part=p)),
            "offers_rejected": _int(find(F, r"Liczba ofert odrzuconych", part=p)),
            "min_price": money(find(F, r"oferty z najniższą ceną", part=p)),
            "max_price": money(find(F, r"oferty z najwyższą ceną", part=p)),
            "winner_price": money(find(F, r"oferty wykonawcy, któremu udzielono", part=p)),
            "contractor": first_line(find(F, r"Nazwa \(firma\) wykonawcy", part=p), 300),
            "contractor_nip": digits(find(F, r"Krajowy Numer Identyfikacyjny", part=p)) if p else "",
            "contract_date": first_line(find(F, r"Data zawarcia umowy", part=p), 20),
            "contract_value": money(find(F, r"Wartość umowy", part=p)),
            "period_txt": first_line(find(F, r"Okres realizacji", part=p), 200),
        }
        part["planned_days"] = parse_period_days(part["period_txt"], parse_date(part["contract_date"]))
        if p == 0 and not part["contractor"]:
            cons = rec.get("contractors") or []
            if cons:
                part["contractor"] = norm_ws(cons[0].get("contractorName") or "")
                part["contractor_nip"] = digits(cons[0].get("contractorNationalId"))
        pn.parts.append(part)
    # opisy części (z sekcji IV) przypisane kolejno
    descs = [f.value for f in F if re.search(r"Krótki opis przedmiotu zamówienia", f.label, re.I)]
    for i, part in enumerate(pn.parts):
        if part["part"] and i < len(descs):
            part["description"] = descs[i][:1500]


def _int(v: str | None) -> int | None:
    m = re.search(r"\d+", v or "")
    return int(m.group(0)) if m else None


def _contract_common(pn: ParsedNotice, F: list[Field]) -> None:
    d = pn.data
    for f in F:
        if re.search(r"Numer ogłoszenia w BZP lub Dz|^Numer ogłoszenia$", f.label) and f.code.startswith("3."):
            d["contract_notice"] = norm_bzp_number(f.value) or first_line(f.value, 60)
    d["eu_funded"] = yes(find(F, r"współfinansowanego ze środków Unii"))
    d["description"] = find(F, r"Krótki opis przedmiotu zamówienia")[:4000]
    d["cpv_main"] = first_line(find(F, r"Główny kod CPV"), 200)
    d["contract_date"] = first_line(find(F, r"Data zawarcia umowy"), 20)
    d["period_txt"] = first_line(find(F, r"Okres realizacji"), 200)
    d["planned_days"] = parse_period_days(d["period_txt"], parse_date(d["contract_date"]))
    d["contractor"] = first_line(find(F, r"Nazwa \(firma\) wykonawcy"), 300)
    d["contractor_nip"] = ""
    for f in F:
        if f.code.startswith("4.3") and re.search(r"Krajowy Numer", f.label):
            d["contractor_nip"] = digits(f.value)[:10]
    d["contract_value"] = money(find(F, r"^Wartość umowy"))
    d["result_notice"] = norm_bzp_number(find(F, r"Numer ogłoszenia o wyniku")) or first_line(find(F, r"Numer ogłoszenia o wyniku"), 60)


def _performing_notice(pn: ParsedNotice, F: list[Field]) -> None:
    _contract_common(pn, F)
    d = pn.data
    d["executed"] = yes(find(F, r"Czy umowa została wykonana:?$|Czy umowa została wykonana$"))
    if d["executed"] is None:
        d["executed"] = yes(find(F, r"^Czy umowa została wykonana"))
    d["completion_date"] = first_line(find(F, r"Termin wykonania umowy"), 20)
    d["on_time"] = yes(find(F, r"wykonano w pierwotnie określonym terminie"))
    d["changes_count"] = _int(find(F, r"Liczba zmian"))
    d["paid_value"] = money(find(F, r"Łączna wartość wynagrodzenia wypłacona"))
    d["properly"] = yes(find(F, r"wykonana należycie"))
    d["not_executed_reason"] = find(F, r"Przyczyny niewykonania|Przyczyna niewykonania|nienależytego")[:1500]
    add = re.search(r"SEKCJA VI INFORMACJE DODATKOWE\s*(.{3,1500}?)(?:\s*\d{4}-\d{2}-\d{2} Biuletyn|\Z)", pn.text, re.S)
    d["additional_info"] = norm_ws(add.group(1)) if add else ""
    # zmiany: bloki zaczynające się od „Numer zmiany”
    cur: dict[str, Any] | None = None
    for f in F:
        lab = f.label
        if re.search(r"Numer zmiany", lab):
            if cur:
                pn.changes.append(cur)
            cur = {"change_no": _int(f.value), "section": f.code}
        elif cur is not None:
            if re.search(r"Podstawa prawna zmiany", lab):
                cur["legal_basis"] = first_line(f.value, 200)
            elif re.search(r"Przyczyny dokonania zmian", lab):
                cur["reason"] = norm_ws(f.value)[:2000]
                cur["reason_section"] = f.code
            elif re.search(r"opis zamówienia po zmianie|opis zmiany", lab, re.I):
                cur["description"] = norm_ws(f.value)[:2000]
            elif re.search(r"Wartość zmiany", lab):
                cur["value"] = money(f.value)
            elif re.search(r"Wzrost ceny", lab):
                cur["price_increase"] = yes(f.value)
            elif re.search(r"Łączna wartość|Czy umowa została wykonana należycie", lab):
                pn.changes.append(cur)
                cur = None
    if cur:
        pn.changes.append(cur)
    for c in pn.changes:
        c["source_type"] = "ContractPerformingNotice"


def _update_notice(pn: ParsedNotice, F: list[Field]) -> None:
    _contract_common(pn, F)
    ch = {
        "change_no": None,
        "change_date": first_line(find(F, r"Data zmiany umowy"), 20),
        "legal_basis": first_line(find(F, r"Podstawa prawna zmiany"), 200),
        "reason": norm_ws(find(F, r"Przyczyny dokonania zmian"))[:2000],
        "description": norm_ws(find(F, r"Krótki opis zmiany"))[:2000],
        "value": money(find(F, r"Wartość zmiany")),
        "price_increase": yes(find(F, r"Wzrost ceny")),
        "source_type": "AgreementUpdateNotice",
    }
    fr = find_field(F, r"Przyczyny dokonania zmian")
    ch["reason_section"] = fr.code if fr else ""
    pn.changes.append(ch)


def basis_label(basis: str | None) -> str:
    """Interpretacja podstawy prawnej zmiany (art. 455 Pzp) wg materiału eksperckiego."""
    b = (basis or "").lower()
    if re.search(r"455\s*ust\.?\s*1\s*pkt\s*1", b):
        return "zmiana przewidziana w dokumentach zamówienia"
    if re.search(r"455\s*ust\.?\s*1\s*pkt\s*2", b):
        return "zmiana wykonawcy"
    if re.search(r"455\s*ust\.?\s*1\s*pkt\s*3", b):
        return "dodatkowe roboty/dostawy/usługi dotychczasowego wykonawcy"
    if re.search(r"455\s*ust\.?\s*1\s*pkt\s*4", b):
        return "okoliczności niemożliwe do przewidzenia"
    if re.search(r"455\s*ust\.?\s*2", b):
        return "zmiana o niewielkiej wartości (limity ustawowe)"
    return ""
