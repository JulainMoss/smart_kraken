#!/usr/bin/env python3
"""
Risk Miner SOTA PoC (v2) — modularny pipeline:
  BZP JSONL -> Multi-lot ETL (SQLite) -> NL Soft-Query (Heuristic lub LLM) ->
  Hybrid RRF (BM25 + Vector + Soft Budget/Facet Boost) -> Empirical Risk Report + JSON Export.

Wymaga wyłącznie biblioteki standardowej Pythona 3.10+.
Konfiguracja opcjonalnego LLM (OpenAI / Ollama / vLLM / OpenRouter):
  export LLM_BASE_URL="http://localhost:11434/v1"   # lub https://api.openai.com/v1
  export LLM_API_KEY="sk-..."
  export LLM_MODEL="gpt-4o-mini"
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sqlite3
import statistics
import urllib.request
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ============================================================================
# 0. KONTRAKT DANYCH
# ============================================================================

@dataclass
class SoftQuery:
    raw_text: str
    subsector: str | None = None
    province: str | None = None
    budget_pln: float | None = None
    risk_hints: list[str] = field(default_factory=list)


@dataclass
class CaseRecord:
    tender_id: str
    org_name: str
    org_nip: str
    province: str
    title: str
    subsector: str
    year: str
    description_snippet: str = ""
    # Agregacja wieloczęściowa (jeden tenderId może mieć wiele umów/części)
    contracts_performed: int = 0
    contracts_executed_yes: int = 0
    contracts_delayed: int = 0
    contracts_improper: int = 0
    total_amendments_reported: int = 0   # z pola 5.4.1.) Liczba zmian
    update_notices_count: int = 0        # z AgreementUpdateNotice
    initial_value_pln: float = 0.0       # suma z 4.4.) Wartość umowy (brutto)
    paid_value_pln: float = 0.0          # suma z 5.5.) Łączna wartość wypłacona (brutto)
    art455_bases: list[str] = field(default_factory=list)
    change_quotes: list[dict[str, str]] = field(default_factory=list)
    notice_numbers: list[str] = field(default_factory=list)

    @property
    def cost_variance_pct(self) -> float | None:
        if self.initial_value_pln > 1000 and self.paid_value_pln > 0:
            return round((self.paid_value_pln - self.initial_value_pln) / self.initial_value_pln * 100.0, 2)
        return None

    @property
    def has_any_issue(self) -> bool:
        return (
            self.contracts_delayed > 0
            or self.contracts_improper > 0
            or self.total_amendments_reported > 0
            or self.update_notices_count > 0
            or (self.cost_variance_pct is not None and self.cost_variance_pct > 2.0)
        )

    @property
    def searchable_text(self) -> str:
        quotes = " ".join(q["quote"] for q in self.change_quotes)
        return f"{self.title} {self.description_snippet} {self.org_name} {self.subsector} {quotes}".strip()


# ============================================================================
# 1. DETERMINISTYCZNY PARSER BZP (ZWERYFIKOWANY NA REALNYCH FORMULARZACH)
# ============================================================================

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
TOKEN_RE = re.compile(r"[a-ząćęłńóśźż0-9]{2,}", re.IGNORECASE)

# Sekcja IV & V w ContractPerformingNotice
RE_INITIAL_VAL = re.compile(
    r"4\.4\.?\)\s*Wartość umowy[^:]*:\s*([\d\s]+[.,]\d{2})\s*PLN", re.IGNORECASE
)
RE_EXECUTED = re.compile(
    r"5\.1\.?\)\s*Czy umowa została wykonana\s*[:?]\s*(Tak|Nie)", re.IGNORECASE
)
RE_ON_TIME = re.compile(
    r"5\.3\.?\)\s*Czy umowę wykonano w pierwotnie określonym terminie\s*[:?]\s*(Tak|Nie)", re.IGNORECASE
)
RE_AMEND_CNT = re.compile(
    r"5\.4\.1\.?\)\s*Liczba zmian\s*[:?]\s*(\d+)", re.IGNORECASE
)
RE_PAID_VAL = re.compile(
    r"5\.5\.?\)\s*Łączna wartość wynagrodzenia wypłacona[^:]*:\s*([\d\s]+[.,]\d{2})\s*PLN", re.IGNORECASE
)
RE_PROPER = re.compile(
    r"5\.6\.?\)\s*Czy umowa została wykonana należycie\s*[:?]\s*(Tak|Nie)", re.IGNORECASE
)
RE_SEC6_DESC = re.compile(
    r"SEKCJA VI\s*INFORMACJE DODATKOWE\s*(.{20,450})", re.IGNORECASE
)

# AgreementUpdateNotice
RE_ART455 = re.compile(r"art\.\s*455\s*ust\.\s*(\d)(?:\s*pkt\s*(\d))?", re.IGNORECASE)
RE_CHANGE_REASON = re.compile(
    r"(?:Przyczyny dokonania zmian|Krótki opis zmiany|Opis zmiany)[^:?]{0,30}[:?]\s*(.{20,400}?)(?:\s*\d+\.\d+\.|\s*SEKCJA|\Z)",
    re.IGNORECASE,
)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", raw))).strip()


def parse_pln(raw: str) -> float:
    try:
        return float(raw.replace(" ", "").replace("\xa0", "").replace(",", "."))
    except ValueError:
        return 0.0


def infer_subsector(text: str) -> str:
    t = text.lower()
    if "oczyszczaln" in t:
        return "oczyszczalnia"
    if "stacji uzdatniania" in t or " suw" in t or "ujęci" in t:
        return "suw"
    if "deszczow" in t or "retencj" in t:
        return "deszczowa"
    if "kanalizac" in t or "ściek" in t or "przepompown" in t:
        return "kanalizacja"
    if "wodociąg" in t or "wodociag" in t or "sieci wod" in t:
        return "wodociag"
    return "inne_wodkan"


def parse_bzp_jsonl(path: Path, max_notices: int | None = None) -> list[CaseRecord]:
    cases: dict[str, CaseRecord] = {}
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if max_notices and i > max_notices:
                break
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            tid = rec.get("tenderId")
            if not tid:
                continue

            title = WS_RE.sub(" ", rec.get("orderObject") or "").strip()
            ntype = rec.get("noticeType") or ""
            nnum = rec.get("noticeNumber") or ""

            if tid not in cases:
                cases[tid] = CaseRecord(
                    tender_id=tid,
                    org_name=WS_RE.sub(" ", rec.get("organizationName") or "").strip(),
                    org_nip=re.sub(r"\D+", "", rec.get("organizationNationalId") or ""),
                    province=rec.get("organizationProvince") or "",
                    title=title,
                    subsector=infer_subsector(title),
                    year=(rec.get("publicationDate") or "")[:4],
                )
            c = cases[tid]
            if not c.title and title:
                c.title = title
                c.subsector = infer_subsector(title)
            if nnum and nnum not in c.notice_numbers:
                c.notice_numbers.append(nnum)

            if ntype == "ContractPerformingNotice":
                c.contracts_performed += 1
                txt = strip_html(rec.get("htmlBody") or "")
                if m := RE_EXECUTED.search(txt):
                    if m.group(1).lower() == "tak":
                        c.contracts_executed_yes += 1
                if m := RE_ON_TIME.search(txt):
                    if m.group(1).lower() == "nie":
                        c.contracts_delayed += 1
                if m := RE_PROPER.search(txt):
                    if m.group(1).lower() == "nie":
                        c.contracts_improper += 1
                if m := RE_AMEND_CNT.search(txt):
                    c.total_amendments_reported += int(m.group(1))
                if m := RE_INITIAL_VAL.search(txt):
                    c.initial_value_pln += parse_pln(m.group(1))
                if m := RE_PAID_VAL.search(txt):
                    c.paid_value_pln += parse_pln(m.group(1))
                if not c.description_snippet:
                    if m := RE_SEC6_DESC.search(txt):
                        c.description_snippet = m.group(1).strip()

            elif ntype == "AgreementUpdateNotice":
                c.update_notices_count += 1
                txt = strip_html(rec.get("htmlBody") or "")
                for m in RE_ART455.finditer(txt):
                    base = f"art. 455 ust. {m.group(1)}" + (f" pkt {m.group(2)}" if m.group(2) else "")
                    if base not in c.art455_bases:
                        c.art455_bases.append(base)
                if m := RE_CHANGE_REASON.search(txt):
                    c.change_quotes.append({"noticeNumber": nnum, "quote": m.group(1).strip()})

    return [c for c in cases.values() if c.contracts_performed > 0 or c.update_notices_count > 0]


# ============================================================================
# 2. BAZA DANYCH (SQLite — interfejs gotowy do podmiany na PostgreSQL+pgvector)
# ============================================================================

class CaseStore:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                tender_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def save_cases(self, records: list[CaseRecord]) -> None:
        rows = [(c.tender_id, json.dumps(asdict(c), ensure_ascii=False)) for c in records]
        self.conn.executemany("INSERT OR REPLACE INTO cases VALUES (?, ?)", rows)
        self.conn.commit()

    def load_all(self) -> list[CaseRecord]:
        cur = self.conn.execute("SELECT payload_json FROM cases")
        return [CaseRecord(**json.loads(r["payload_json"])) for r in cur.fetchall()]


# ============================================================================
# 3. NL -> SOFT QUERY (HEURYSTYKA OFFLINE LUB DOWOLNY LLM Z KONFIGURACJI)
# ============================================================================

HINT_MAP = {
    "grunty": ("działk", "prywatn", "właściciel", "służebnoś", "prawa do dysponowania", "wywłaszcz"),
    "kolizje": ("kolizj", "uzbrojeni", "przekroczeni", "ciek", "kolej", "gazociąg", "energetyczn"),
    "zaprojektuj": ("zaprojektuj", "pfu", "formule", "projektuj i buduj"),
    "srodowisko": ("środowisk", "natura 2000", "wodnoprawn", "archeolog"),
    "geotechnika": ("geolog", "geotechni", "gruntowo-wodn", "podmokł", "odwodnieni"),
}


class BaseQueryParser(ABC):
    @abstractmethod
    def parse(self, user_query: str) -> SoftQuery: ...


class HeuristicQueryParser(BaseQueryParser):
    PROV_MAP = {
        "dolnośląsk": "PL02", "kujawsko": "PL04", "lubelsk": "PL06", "lubusk": "PL08",
        "łódzk": "PL10", "małopolsk": "PL12", "mazowieck": "PL14", "opolsk": "PL16",
        "podkarpack": "PL18", "podlask": "PL20", "pomorsk": "PL22", "śląsk": "PL24",
        "świętokrzysk": "PL26", "warmińsko": "PL28", "wielkopolsk": "PL30", "zachodniopomorsk": "PL32",
    }
    RE_BUDGET_MLN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:mln|milion)", re.IGNORECASE)
    RE_BUDGET_TYS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:tys|k\b)", re.IGNORECASE)

    def parse(self, user_query: str) -> SoftQuery:
        q = user_query.lower()
        sub = infer_subsector(q)
        prov = next((code for k, code in self.PROV_MAP.items() if k in q), None)
        hints = [k for k, words in HINT_MAP.items() if any(w in q for w in words)]

        budget = None
        if m := self.RE_BUDGET_MLN.search(q):
            budget = float(m.group(1).replace(",", ".")) * 1_000_000
        elif m := self.RE_BUDGET_TYS.search(q):
            budget = float(m.group(1).replace(",", ".")) * 1_000

        return SoftQuery(
            raw_text=user_query,
            subsector=sub if sub != "inne_wodkan" else None,
            province=prov,
            budget_pln=budget,
            risk_hints=hints,
        )


class OpenAICompatibleQueryParser(BaseQueryParser):
    """Konfigurowalny parser LLM (OpenAI / OpenRouter / lokalny Ollama/vLLM) bez zewnętrznych bibliotek."""
    def __init__(self) -> None:
        self.base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "gpt-4o-mini")

    def parse(self, user_query: str) -> SoftQuery:
        prompt = (
            "Wyekstrahuj z opisu inwestycji miękkie preferencje w czystym JSON (bez markdown): "
            '{"subsector": "wodociag|kanalizacja|oczyszczalnia|suw|deszczowa|null", '
            '"province": "PL02..PL32 lub null", "budget_pln": liczba lub null, '
            '"risk_hints": lista z ["grunty","kolizje","zaprojektuj","srodowisko","geotechnika"]}.'
        )
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_query},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        parsed = json.loads(data["choices"][0]["message"]["content"])
        return SoftQuery(
            raw_text=user_query,
            subsector=parsed.get("subsector"),
            province=parsed.get("province"),
            budget_pln=parsed.get("budget_pln"),
            risk_hints=parsed.get("risk_hints") or [],
        )


# ============================================================================
# 4. HYBRYDOWY RETRIEVER (BM25 + EMBEDDING + SOFT FILTERS + LOG-BUDGET + RRF)
# ============================================================================

class BaseEmbedder(ABC):
    @abstractmethod
    def embed(self, text: str) -> dict[str, float]: ...


class CharNgramEmbedder(BaseEmbedder):
    def embed(self, text: str) -> dict[str, float]:
        clean = WS_RE.sub(" ", text.lower())
        grams = [clean[i:i+4] for i in range(max(0, len(clean) - 3))]
        counts = Counter(grams)
        norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
        return {k: v / norm for k, v in counts.items()}


def cosine_sparse(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


class HybridRetriever:
    def __init__(self, cases: list[CaseRecord], embedder: BaseEmbedder):
        self.cases = cases
        self.embedder = embedder
        self.tokenized = [TOKEN_RE.findall(c.searchable_text.lower()) for c in cases]
        self.avg_dl = sum(len(t) for t in self.tokenized) / max(1, len(self.tokenized))
        self.df: Counter[str] = Counter()
        for toks in self.tokenized:
            for tok in set(toks):
                self.df[tok] += 1
        self.vectors = [embedder.embed(c.searchable_text) for c in cases]

    def _bm25_scores(self, query_text: str, k1: float = 1.5, b: float = 0.75) -> list[float]:
        q_toks = TOKEN_RE.findall(query_text.lower())
        N = len(self.cases)
        scores = []
        for toks in self.tokenized:
            tf = Counter(toks)
            dl = len(toks)
            s = 0.0
            for qt in q_toks:
                if qt not in tf:
                    continue
                idf = math.log((N - self.df[qt] + 0.5) / (self.df[qt] + 0.5) + 1.0)
                freq = tf[qt]
                s += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / self.avg_dl))
            scores.append(s)
        return scores

    def _soft_score(self, case: CaseRecord, sq: SoftQuery) -> tuple[float, list[str]]:
        score = 0.0
        reasons = []
        if sq.subsector and case.subsector == sq.subsector:
            score += 2.0
            reasons.append(f"podsektor={case.subsector}")
        if sq.province and case.province == sq.province:
            score += 0.7
            reasons.append(f"woj={case.province}")
        # Logarytmiczna bliskość budżetu (zapobiega dryfowi skali!)
        case_val = case.paid_value_pln or case.initial_value_pln
        if sq.budget_pln and case_val > 1000:
            log_diff = abs(math.log10(sq.budget_pln) - math.log10(case_val))
            b_sim = math.exp(-log_diff * 1.5)
            score += 1.5 * b_sim
            if b_sim >= 0.45:
                reasons.append(f"skala_budżetu={case_val/1e6:.2f}M")
        txt = case.searchable_text.lower()
        for hint in sq.risk_hints:
            if any(w in txt for w in HINT_MAP.get(hint, ())):
                score += 1.2
                reasons.append(f"sygnał={hint}")
        return score, reasons

    def search(self, sq: SoftQuery, top_k: int = 12, rrf_k: int = 60) -> list[dict[str, Any]]:
        bm25 = self._bm25_scores(sq.raw_text)
        q_vec = self.embedder.embed(sq.raw_text)
        vec = [cosine_sparse(q_vec, v) for v in self.vectors]
        soft_res = [self._soft_score(c, sq) for c in self.cases]
        soft = [s[0] for s in soft_res]

        def to_ranks(vals: list[float]) -> dict[int, int]:
            order = sorted(range(len(vals)), key=lambda idx: vals[idx], reverse=True)
            return {idx: rank + 1 for rank, idx in enumerate(order)}

        r_bm25, r_vec, r_soft = to_ranks(bm25), to_ranks(vec), to_ranks(soft)

        fused = []
        for idx, case in enumerate(self.cases):
            rrf = (
                1.0 / (rrf_k + r_bm25[idx])
                + 1.0 / (rrf_k + r_vec[idx])
                + 0.9 / (rrf_k + r_soft[idx])
            )
            fused.append({
                "case": case,
                "rrf_score": round(rrf, 5),
                "explain": {
                    "bm25_rank": r_bm25[idx],
                    "vec_rank": r_vec[idx],
                    "soft_matches": soft_res[idx][1],
                },
            })
        fused.sort(key=lambda x: x["rrf_score"], reverse=True)
        return fused[:top_k]


# ============================================================================
# 5. EMPIRYCZNY RAPORT RYZYK + EKSPORT JSON
# ============================================================================

def build_report(sq: SoftQuery, results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    cases = [r["case"] for r in results]
    delayed = [c for c in cases if c.contracts_delayed > 0]
    amended = [c for c in cases if c.total_amendments_reported > 0 or c.update_notices_count > 0]
    improper = [c for c in cases if c.contracts_improper > 0]
    successes = [c for c in cases if not c.has_any_issue and c.contracts_performed > 0]
    variances = [c.cost_variance_pct for c in cases if c.cost_variance_pct is not None]
    art_counts = Counter(b for c in cases for b in c.art455_bases)

    report = {
        "query": asdict(sq),
        "sample_size": n,
        "empirical_base_rates": {
            "delayed_count": len(delayed),
            "delayed_pct": round(100 * len(delayed) / max(1, n), 1),
            "amended_count": len(amended),
            "amended_pct": round(100 * len(amended) / max(1, n), 1),
            "improper_count": len(improper),
            "clean_success_count": len(successes),
            "cost_variance_sample_n": len(variances),
            "cost_variance_median_pct": round(statistics.median(variances), 2) if variances else None,
            "cost_variance_max_pct": round(max(variances), 2) if variances else None,
            "top_art455_bases": dict(art_counts.most_common(5)),
        },
        "peers_to_contact": [
            {"org_name": c.org_name, "org_nip": c.org_nip, "province": c.province, "title": c.title}
            for c in successes[:3]
        ],
        "comparable_cases": [
            {
                "rrf_score": r["rrf_score"],
                "explain": r["explain"],
                "tender_id": r["case"].tender_id,
                "org_name": r["case"].org_name,
                "org_nip": r["case"].org_nip,
                "province": r["case"].province,
                "year": r["case"].year,
                "title": r["case"].title,
                "paid_value_pln": r["case"].paid_value_pln,
                "cost_variance_pct": r["case"].cost_variance_pct,
                "contracts_delayed": r["case"].contracts_delayed,
                "total_amendments": max(r["case"].total_amendments_reported, r["case"].update_notices_count),
                "change_quotes": r["case"].change_quotes[:2],
                "notice_numbers": r["case"].notice_numbers,
            }
            for r in results
        ],
    }
    return report


def print_report(rep: dict[str, Any]) -> None:
    q = rep["query"]
    br = rep["empirical_base_rates"]
    n = rep["sample_size"]
    print("\n" + "=" * 80)
    print(f"ZAPYTANIE: {q['raw_text']}")
    print(f"MIĘKKIE FILTRY: podsektor={q['subsector']}, woj={q['province']}, budżet={q['budget_pln']}, sygnały={q['risk_hints']}")
    print("=" * 80)
    print(f"EMPIRYCZNE STATYSTYKI GRUPY PORÓWNAWCZEJ (N={n}):")
    print(f"  • Opóźnienia (po pierwotnym terminie):  {br['delayed_count']}/{n} ({br['delayed_pct']}%)")
    print(f"  • Aneksowane umowy (5.4.1 lub Update):  {br['amended_count']}/{n} ({br['amended_pct']}%)")
    print(f"  • Nienależyte wykonanie (5.6=Nie):      {br['improper_count']}/{n}")
    print(f"  • Czyste sukcesy (grupa kontrolna):     {br['clean_success_count']}/{n}")
    if br["cost_variance_sample_n"]:
        print(
            f"  • Odchylenie kosztu brutto (n={br['cost_variance_sample_n']}): "
            f"mediana={br['cost_variance_median_pct']:+.2f}%, maks={br['cost_variance_max_pct']:+.2f}%"
        )
    if br["top_art455_bases"]:
        print(f"  • Podstawy zmian (art. 455 Pzp):        {br['top_art455_bases']}")

    print("\nTOP PODOBNE INWESTYCJE:")
    for i, c in enumerate(rep["comparable_cases"], 1):
        val = f"{c['paid_value_pln']/1e6:.2f}M PLN" if c["paid_value_pln"] else "brak kwoty"
        var = f"{c['cost_variance_pct']:+.1f}%" if c["cost_variance_pct"] is not None else "b/d"
        print(f"\n[{i}] {c['title'][:100]}")
        print(f"    {c['org_name']} ({c['province']}, {c['year']}) | {val} (odchylenie: {var}) | opóźn={c['contracts_delayed']}, aneksy={c['total_amendments']}")
        print(f"    RRF={c['rrf_score']} (BM25=#{c['explain']['bm25_rank']}, Vec=#{c['explain']['vec_rank']}, Soft={c['explain']['soft_matches']})")
        for dq in c["change_quotes"]:
            print(f"    -> DOWÓD [{dq['noticeNumber']}]: \"{dq['quote'][:220]}\"")

    if rep["peers_to_contact"]:
        print("\nDO KOGO SIĘ ODEZWAĆ (zrealizowane bez opóźnień i aneksów):")
        for p in rep["peers_to_contact"]:
            print(f"  • {p['org_name']} (NIP: {p['org_nip']}, {p['province']}) — \"{p['title'][:75]}\"")


# ============================================================================
# CLI
# ============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Risk Miner SOTA PoC (v2)")
    ap.add_argument("--jsonl", type=Path, default=Path("bzp_kopia/notices.jsonl"))
    ap.add_argument("--db", type=Path, default=Path("risk_miner_sota.sqlite"))
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--query", type=str)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--use-llm", action="store_true", help="Użyj OpenAICompatibleQueryParser z env")
    ap.add_argument("--export", type=Path, help="Zapisz ustrukturyzowany raport do JSON")
    args = ap.parse_args()

    store = CaseStore(args.db)
    if args.ingest or not args.db.exists():
        print(f"Indeksuję {args.jsonl}...")
        records = parse_bzp_jsonl(args.jsonl, max_notices=args.limit)
        store.save_cases(records)
        delayed = sum(1 for r in records if r.contracts_delayed > 0)
        amended = sum(1 for r in records if r.total_amendments_reported > 0 or r.update_notices_count > 0)
        with_var = sum(1 for r in records if r.cost_variance_pct is not None)
        print(
            f"Zapisano {len(records):,} spraw do {args.db} | "
            f"opóźnione: {delayed:,} | aneksowane: {amended:,} | z odchyleniem kosztu: {with_var:,}"
        )

    if args.query:
        cases = store.load_all()
        parser: BaseQueryParser = OpenAICompatibleQueryParser() if args.use_llm else HeuristicQueryParser()
        sq = parser.parse(args.query)
        retriever = HybridRetriever(cases, embedder=CharNgramEmbedder())
        results = retriever.search(sq, top_k=args.top_k)
        report = build_report(sq, results)
        print_report(report)
        if args.export:
            args.export.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nWyeksportowano raport JSON do: {args.export}")


if __name__ == "__main__":
    main()