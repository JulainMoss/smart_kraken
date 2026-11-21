#!/usr/bin/env python3
"""
Risk Miner — Automatyczne zasilanie i wzbogacanie bazy (Excel + Pliki + BZP API + TED API v3 + NIK + Mały LLM).
Wymaga wyłącznie biblioteki standardowej Pythona 3.10+ (opcjonalnie 'pypdf' do plików PDF).

Zmienne środowiskowe:
  BZP_BASE_URL   domyślnie http://127.0.0.1:8010 (na produkcji: https://ezamowienia.gov.pl)
  LLM_BASE_URL   np. https://api.openai.com/v1 lub https://openrouter.ai/api/v1
  LLM_API_KEY    klucz API
  LLM_MODEL      np. gpt-4o-mini / google/gemini-2.5-flash-lite
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
TOKEN_RE = re.compile(r"[a-ząćęłńóśźż0-9]{3,}", re.IGNORECASE)

# Regexy BZP — w tym sekcja 5.4.2/5.4.3 z ContractPerformingNotice (kluczowa dla powodów zmian!)
RE_SEC54_CHANGES = re.compile(
    r"5\.4\.(?:2|3)\.?\)\s*(?:Rodzaj i zakres zmian|Przyczyny dokonania zmian|Krótki opis zmiany)[^:]*:\s*(.{15,500}?)(?=\s*5\.(?:4\.\d|5)\.\)|\s*SEKCJA|\Z)",
    re.IGNORECASE,
)
RE_UPDATE_REASON = re.compile(
    r"(?:Przyczyny dokonania zmian|Krótki opis zmiany|Opis zmiany)[^:?]{0,30}[:?]\s*(.{15,450}?)(?=\s*\d+\.\d+\.|\s*SEKCJA|\Z)",
    re.IGNORECASE,
)
RE_ON_TIME = re.compile(
    r"5\.3\.?\)\s*Czy umowę wykonano w pierwotnie określonym terminie\s*[:?]\s*(Tak|Nie)", re.IGNORECASE
)
RE_AMEND_CNT = re.compile(r"5\.4\.1\.?\)\s*Liczba zmian\s*[:?]\s*(\d+)", re.IGNORECASE)
RE_INITIAL_VAL = re.compile(r"4\.4\.?\)\s*Wartość umowy[^:]*:\s*([\d\s]+[.,]\d{2})\s*PLN", re.IGNORECASE)
RE_PAID_VAL = re.compile(r"5\.5\.?\)\s*Łączna wartość wynagrodzenia wypłacona[^:]*:\s*([\d\s]+[.,]\d{2})\s*PLN", re.IGNORECASE)

RISK_CATALOG_PROMPT = """
Katalog ryzyk MFiPR:
R01 Niepełny lub niespójny zakres | R02 Niedostateczne rozpoznanie stanu istniejącego
R03 Kolizje, dostęp do terenu i prawa do nieruchomości | R04 Decyzje, pozwolenia i uzgodnienia
R05 Niejasny podział odpowiedzialności | R06 Nierealistyczny harmonogram i zależności
R07 Nieprzewidziane zmiany i roboty dodatkowe | R08 Model wynagrodzenia niedopasowany do niepewności
R09 Niejednoznaczne wymagania techniczne | R10 Niedookreślone próby, rozruch i odbiór
R11 Postępowanie podatne na odwołanie | R12 Niewystarczająca zdolność wykonawcy/dostaw
R13 Ryzyko finansowania i budżetu | R14 Nadmierne koszty eksploatacji | R15 Nieosiągnięcie efektu
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def strip_html(raw: str) -> str:
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", raw or ""))).strip()


def normalize_nip(val: str | None) -> str:
    return re.sub(r"\D+", "", str(val or ""))


def parse_pln(raw: str) -> float:
    try:
        return float(raw.replace(" ", "").replace("\xa0", "").replace(",", "."))
    except ValueError:
        return 0.0


def jaccard_tokens(a: str, b: str) -> float:
    sa, sb = set(TOKEN_RE.findall(a.lower())), set(TOKEN_RE.findall(b.lower()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ============================================================================
# 1. BAZA DANYCH (ROZSZERZENIE SCHEMATU O PROJEKTY, LINKI, DOKUMENTY I RYZYKA)
# ============================================================================

class EnrichmentDB:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                org_nip TEXT,
                org_name TEXT,
                province TEXT,
                title TEXT,
                public_id_type TEXT,
                public_id TEXT,
                annotation TEXT,
                source_file TEXT,
                imported_at TEXT
            );
            CREATE TABLE IF NOT EXISTS enrichment_links (
                link_id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT,
                source_system TEXT,     -- BZP / TED / NIK / FILE
                external_id TEXT,       -- tenderId / publication-number / nr NIK
                source_url TEXT,
                match_basis TEXT,       -- exact_notice_number / nip_plus_title / keyword
                confidence REAL,
                payload_json TEXT,
                fetched_at TEXT,
                UNIQUE(project_id, source_system, external_id)
            );
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                project_id TEXT,
                source_system TEXT,
                title TEXT,
                source_url TEXT,
                content_sha256 TEXT,
                extracted_text TEXT,
                archived_at TEXT
            );
            CREATE TABLE IF NOT EXISTS risk_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT,
                source_ref TEXT,
                risk_code TEXT,
                cause TEXT,
                effect TEXT,
                mitigation TEXT,
                source_quote TEXT,
                quote_verified INTEGER,
                confidence REAL,
                verification_status TEXT DEFAULT 'pending_review',
                model_used TEXT,
                created_at TEXT
            );
        """)
        self.conn.commit()


# ============================================================================
# 2. IMPORTER PLIKÓW (.XLSX, .DOCX, .TXT, .PDF) — BEZ ZEW. ZALEŻNOŚCI DLA XLSX/DOCX
# ============================================================================

def _col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    """Czyta pierwszy arkusz .xlsx czystą biblioteką standardową (obsługuje puste komórki)."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.findall(".//m:t", ns)))
        sheet = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        rows_raw: list[list[str]] = []
        for row in sheet.findall(".//m:sheetData/m:row", ns):
            cells: dict[int, str] = {}
            for c in row.findall("m:c", ns):
                col = _col_index(c.attrib.get("r", "A1"))
                ctype = c.attrib.get("t", "")
                val_el = c.find("m:v", ns)
                if ctype == "s" and val_el is not None:
                    cells[col] = shared[int(val_el.text or "0")]
                elif ctype == "inlineStr":
                    cells[col] = "".join(t.text or "" for t in c.findall(".//m:t", ns))
                elif val_el is not None and val_el.text is not None:
                    cells[col] = val_el.text
            if cells:
                max_c = max(cells)
                rows_raw.append([cells.get(i, "") for i in range(max_c + 1)])
    if not rows_raw:
        return []
    headers = [h.strip() for h in rows_raw[0]]
    return [
        {headers[i]: (r[i].strip() if i < len(r) else "") for i in range(len(headers))}
        for r in rows_raw[1:]
        if any(r)
    ]


def read_document_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".txt", ".md", ".csv", ".json"):
        return path.read_text(encoding="utf-8", errors="ignore")
    if ext == ".docx":
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paras = [
                "".join(t.text or "" for t in p.findall(".//w:t", ns))
                for p in root.findall(".//w:p", ns)
            ]
            return "\n".join(p for p in paras if p.strip())
    if ext == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore
            return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
        except ImportError:
            raise SystemExit("Do plików PDF zainstaluj: pip install pypdf")
    raise SystemExit(f"Nieobsługiwany format pliku: {ext}")


# ============================================================================
# 3. KONEKTORY ŹRÓDEŁ PUBLICZNYCH (BZP MIRROR/PROD, TED API v3, NIK)
# ============================================================================

class BZPConnector:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("BZP_BASE_URL", "http://127.0.0.1:8010")).rstrip("/")
        self.endpoint = f"{self.base_url}/mo-board/api/v1/notice"

    def _get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def enrich_project(self, proj: dict[str, str]) -> list[dict[str, Any]]:
        """Łączy projekt z Excela z ogłoszeniami BZP (exact match lub NIP + podobieństwo tytułu)."""
        pub_type = proj.get("public_id_type", "")
        pub_id = proj.get("public_id", "")
        nip = normalize_nip(proj.get("org_nip"))
        org_name = proj.get("org_name", "")
        title = proj.get("title", "")

        matched_tender_ids: dict[str, tuple[str, float]] = {}  # tenderId -> (basis, confidence)

        # Ścieżka 1: Dokładny numer ogłoszenia BZP
        if "BZP" in pub_type.upper() and pub_id:
            for ntype in ("ContractNotice", "TenderResultNotice", "ContractPerformingNotice"):
                items = self._get({"NoticeType": ntype, "NoticeNumber": pub_id, "PageSize": 5})
                for it in items:
                    if tid := it.get("tenderId"):
                        matched_tender_ids[tid] = ("exact_notice_number", 1.0)
                if matched_tender_ids:
                    break

        # Ścieżka 2: Projekt KPO lub brak trafienia po numerze -> NIP + podobieństwo nazwy
        if not matched_tender_ids and org_name:
            short_org = org_name.split("–")[0].strip()
            candidates = self._get({"NoticeType": "ContractNotice", "OrganizationName": short_org, "PageSize": 50})
            scored = []
            for it in candidates:
                if nip and normalize_nip(it.get("organizationNationalId")) != nip:
                    continue
                sim = jaccard_tokens(title, it.get("orderObject") or "")
                if sim >= 0.25:
                    scored.append((sim, it.get("tenderId")))
            scored.sort(reverse=True)
            if scored and scored[0][1]:
                conf = round(min(0.95, 0.55 + scored[0][0] * 0.5), 2)
                matched_tender_ids[scored[0][1]] = (f"nip_plus_title_jaccard({scored[0][0]:.2f})", conf)

        # Dla dopasowanych tenderId pobieramy ogłoszenia wykonania i zmian umowy
        results = []
        if not matched_tender_ids:
            return results

        short_org = org_name.split("–")[0].strip()
        related_notices: list[dict[str, Any]] = []
        for ntype in ("ContractPerformingNotice", "AgreementUpdateNotice"):
            items = self._get({"NoticeType": ntype, "OrganizationName": short_org, "PageSize": 100})
            for it in items:
                if it.get("tenderId") in matched_tender_ids:
                    related_notices.append(it)

        for tid, (basis, conf) in matched_tender_ids.items():
            t_notices = [n for n in related_notices if n.get("tenderId") == tid]
            quotes = []
            delayed = False
            amendments = 0
            init_val, paid_val = 0.0, 0.0

            for n in t_notices:
                txt = strip_html(n.get("htmlBody") or "")
                nnum = n.get("noticeNumber") or ""
                if n.get("noticeType") == "ContractPerformingNotice":
                    if m := RE_ON_TIME.search(txt):
                        if m.group(1).lower() == "nie":
                            delayed = True
                    if m := RE_AMEND_CNT.search(txt):
                        amendments += int(m.group(1))
                    if m := RE_INITIAL_VAL.search(txt):
                        init_val += parse_pln(m.group(1))
                    if m := RE_PAID_VAL.search(txt):
                        paid_val += parse_pln(m.group(1))
                    for m in RE_SEC54_CHANGES.finditer(txt):
                        quotes.append({"notice": nnum, "section": "5.4.2/5.4.3", "quote": m.group(1).strip()})
                elif n.get("noticeType") == "AgreementUpdateNotice":
                    for m in RE_UPDATE_REASON.finditer(txt):
                        quotes.append({"notice": nnum, "section": "AgreementUpdate", "quote": m.group(1).strip()})

            results.append({
                "source_system": "BZP",
                "external_id": tid,
                "source_url": f"https://ezamowienia.gov.pl/mp-client/search/list/{tid}",
                "match_basis": basis,
                "confidence": conf,
                "payload": {
                    "notices_linked": len(t_notices),
                    "delayed": delayed,
                    "amendments_count": amendments,
                    "initial_value_pln": init_val,
                    "paid_value_pln": paid_val,
                    "change_quotes": quotes,
                },
            })
        return results


class TEDConnector:
    """Darmowe, bezkluczowe TED Search API v3 (Unia Europejska)."""
    URL = "https://api.ted.europa.eu/v3/notices/search"

    def search_water_pl(self, keyword: str = "kanalizac", limit: int = 5) -> list[dict[str, Any]]:
        body = json.dumps({
            "query": f'buyer-country=POL AND classification-cpv=45231300 AND FT~"{keyword}" SORT BY publication-date DESC',
            "fields": ["publication-number", "notice-title", "buyer-name", "notice-type", "publication-date"],
            "scope": "ALL",
            "limit": limit,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.URL, data=body, headers={"Content-Type": "application/json", "User-Agent": "RiskMiner/1.0"}
        )