#!/usr/bin/env python3
"""
Szybkie, strumieniowe podsumowanie lokalnej kopii BZP (bzp_kopia/notices.jsonl).
Wymaga wyłącznie biblioteki standardowej Pythona 3.10+.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# 20 unikalnych NIP-ów z pliku Risk_Miner_dane_projekty.xlsx
TEST_NIPS = {
    "9121715754": "Gmina Oława",
    "6941566139": "Gmina Zagrodno",
    "8792466869": "Gmina Czernikowo",
    "8792469951": "Gmina Zławieś Wielka",
    "7151863553": "Gmina Gościeradów",
    "7141896261": "Gmina Kamionka",
    "7381973883": "Gmina Biecz",
    "7962959318": "Gmina Zakrzew",
    "8161570014": "Gmina Grodzisko Dolne",
    "6871783356": "Gmina Sanok",
    "5890010583": "Gmina Kartuzy",
    "6521708710": "Gmina Bestwina",
    "6252446477": "Gmina Bobrowniki",
    "5783105254": "Gmina Elbląg",
    "7412025674": "Gmina Miłakowo",
    "7773157115": "Gmina Kleszczewo",
    "6211694095": "Gmina Zduny",
    "9720914891": "WZDW Poznań",
    "8590012007": "Gmina Nowogard",
    "9860164852": "Gmina Wolin",
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
NIP_CLEAN_RE = re.compile(r"\D+")

# Wzorce sygnałów ryzyka w oczyszczonym tekście htmlBody
RE_ON_TIME = re.compile(
    r"wykonana w pierwotnie określonym terminie[^:]*:\s*(Tak|Nie)", re.IGNORECASE
)
RE_CHANGED = re.compile(
    r"Czy umowa została zmieniona[^:]*:\s*(Tak|Nie)", re.IGNORECASE
)
RE_PROPER = re.compile(
    r"Czy umowę wykonano należycie[^:]*:\s*(Tak|Nie)", re.IGNORECASE
)
RE_ART455 = re.compile(
    r"art\.\s*455\s*ust\.\s*(\d)(?:\s*pkt\s*(\d))?", re.IGNORECASE
)
RE_CHANGE_REASON = re.compile(
    r"(?:Przyczyny dokonania zmian|Krótki opis zmiany|Opis zmiany)[^:]*:\s*(.{20,350}?)(?:\s*\d+\.\d+\.|\s*SEKCJA|\Z)",
    re.IGNORECASE,
)


def strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = TAG_RE.sub(" ", raw_html)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def normalize_nip(val: str | None) -> str:
    return NIP_CLEAN_RE.sub("", val or "")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("bzp_kopia/notices.jsonl")
    if not path.exists():
        raise SystemExit(f"Nie znaleziono pliku: {path}. Podaj ścieżkę jako argument.")

    t0 = time.monotonic()
    total = 0
    by_type: Counter[str] = Counter()
    by_year: Counter[str] = Counter()
    by_prov: Counter[str] = Counter()
    tender_types: dict[str, set[str]] = defaultdict(set)

    # Pokrycie podmiotów testowych
    test_hits: dict[str, Counter[str]] = defaultdict(Counter)

    # Sygnały ryzyka z htmlBody
    perf_on_time: Counter[str] = Counter()
    perf_changed: Counter[str] = Counter()
    perf_proper: Counter[str] = Counter()
    art455_counts: Counter[str] = Counter()
    sample_reasons: list[tuple[str, str, str]] = []

    print(f"Skanuję strumieniowo {path} ({path.stat().st_size / 2**30:.2f} GB)...")

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            total += 1

            ntype = rec.get("noticeType") or "UNKNOWN"
            pub_year = (rec.get("publicationDate") or "")[:4] or "?"
            prov = rec.get("organizationProvince") or "?"
            tid = rec.get("tenderId")

            by_type[ntype] += 1
            by_year[pub_year] += 1
            by_prov[prov] += 1
            if tid:
                tender_types[tid].add(ntype)

            nip = normalize_nip(rec.get("organizationNationalId"))
            if nip in TEST_NIPS:
                test_hits[nip][ntype] += 1

            # Parsujemy htmlBody tylko tam, gdzie są twarde sygnały realizacji i zmian
            if ntype == "ContractPerformingNotice":
                txt = strip_html(rec.get("htmlBody") or "")
                if m := RE_ON_TIME.search(txt):
                    perf_on_time[m.group(1).capitalize()] += 1
                if m := RE_CHANGED.search(txt):
                    perf_changed[m.group(1).capitalize()] += 1
                if m := RE_PROPER.search(txt):
                    perf_proper[m.group(1).capitalize()] += 1

            elif ntype == "AgreementUpdateNotice":
                txt = strip_html(rec.get("htmlBody") or "")
                for m in RE_ART455.finditer(txt):
                    ust, pkt = m.group(1), m.group(2)
                    key = f"art. 455 ust. {ust}" + (f" pkt {pkt}" if pkt else "")
                    art455_counts[key] += 1
                if len(sample_reasons) < 5:
                    if m := RE_CHANGE_REASON.search(txt):
                        sample_reasons.append(
                            (
                                rec.get("noticeNumber") or "?",
                                rec.get("organizationName") or "?",
                                m.group(1).strip(),
                            )
                        )

            if total % 25000 == 0:
                print(f"  przetworzono {total:,} rekordów...", flush=True)

    dt = time.monotonic() - t0
    print(f"\n=== PODSUMOWANIE ({total:,} ogłoszeń w {dt:.1f} s) ===")

    print("\n1. Typy ogłoszeń:")
    for k, v in by_type.most_common():
        print(f"   {k:28s}: {v:>7,}")

    print("\n2. Rozkład roczny:")
    for k in sorted(by_year):
        print(f"   {k}: {by_year[k]:>7,}")

    # Analiza pełności cyklu życia (tenderId)
    full_chain = sum(
        1
        for s in tender_types.values()
        if {"ContractNotice", "TenderResultNotice", "ContractPerformingNotice"} <= s
    )
    with_update = sum(
        1 for s in tender_types.values() if "AgreementUpdateNotice" in s
    )
    full_plus_update = sum(
        1
        for s in tender_types.values()
        if {
            "ContractNotice",
            "TenderResultNotice",
            "AgreementUpdateNotice",
            "ContractPerformingNotice",
        }
        <= s
    )
    print(f"\n3. Łączenie po tenderId (unikalnych postępowań: {len(tender_types):,}):")
    print(f"   Pełny cykl (Ogłoszenie + Wynik + Wykonanie):           {full_chain:>7,}")
    print(f"   Postępowania z ogłoszeniem o zmianie umowy (aneks):    {with_update:>7,}")
    print(f"   Pełny cykl + aneks (złoty zbiór treningowy/empiryczny):{full_plus_update:>7,}")

    print("\n4. Sygnały ryzyka z ContractPerformingNotice (wykonanie umowy):")
    print(f"   Wykonano w pierwotnym terminie: {dict(perf_on_time)}")
    print(f"   Umowa była zmieniana (aneksy):  {dict(perf_changed)}")
    print(f"   Wykonano należycie:             {dict(perf_proper)}")

    print("\n5. Podstawy prawne zmian z AgreementUpdateNotice (art. 455 Pzp):")
    for k, v in art455_counts.most_common(8):
        print(f"   {k:25s}: {v:>6,}")

    print("\n6. Pokrycie 20 podmiotów testowych z Excela (po NIP):")
    found_nips = 0
    for nip, name in TEST_NIPS.items():
        c = test_hits.get(nip)
        if c:
            found_nips += 1
            summary = ", ".join(f"{k[:4]}={v}" for k, v in c.most_common())
            print(f"   {name:24s} ({nip}): {sum(c.values()):>3} ogł. [{summary}]")
        else:
            print(f"   {name:24s} ({nip}):   0 ogł.")
    print(f"   --> Znaleziono ogłoszenia dla {found_nips}/{len(TEST_NIPS)} podmiotów testowych.")

    print("\n7. Przykładowe przyczyny zmian wycięte z AgreementUpdateNotice:")
    for num, org, reason in sample_reasons:
        print(f"   - [{num}] {org}:")
        print(f"     \"{reason}\"")


if __name__ == "__main__":
    main()