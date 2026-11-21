#!/usr/bin/env python3
"""
Risk Miner — modularny prototyp PoC (ETL -> SQLite -> NL Soft-Query -> Hybrid RRF -> Risk Report).
Wymaga wyłącznie biblioteki standardowej Pythona 3.10+.
Każdy moduł (QueryParser, Embedder, SoftScorer) można wymienić niezależnie.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sqlite3
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ============================================================================
# 0. MODELE DANYCH (KONTRAKT MIĘDZY MODUŁAMI)
# ============================================================================

@dataclass
class SoftQuery:
    """Wynik zamiany kwerendy NL urzędnika na miękkie preferencje (nigdy nie odrzuca rekordów)."""
    raw_text: str
    subsector: str | None = None        # np. 'wodociag', 'kanalizacja', 'oczyszczalnia', 'suw'
    province: str | None = None         # np. 'PL18'
    risk_hints: list[str] = field(default_factory=list)  # np. ['grunty', 'kolizje', 'zaprojektuj']


@dataclass
class CaseRecord:
    tender_id: str
    org_name: str
    org_nip: str
    province: str
    title: str
    subsector: str
    year: str
    has_contract_notice: bool = False
    has_result_notice: bool = False
    has_performing_notice: bool = False
    update_count: int = 0
    executed_on_time: str | None = None     # 'Tak' / 'Nie' / None
    was_modified: str | None = None         # 'Tak' / 'Nie' / None
    executed_properly: str | None = None    # 'Tak' / 'Nie' / None
    art455_bases: list[str] = field(default_factory=list)
    change_quotes: list[dict[str, str]] = field(default_factory=list)  # [{noticeNumber, quote}]
    notice_numbers: list[str] = field(default_factory=list)

    @property
    def searchable_text(self) -> str:
        quotes = " ".join(q["quote"] for q in self.change_quotes)
        return f"{self.title} {self.org_name} {self.subsector} {quotes}".strip()


# ============================================================================
# 1. MODUŁ PARSOWANIA BZP (bzp_kopia/notices.jsonl -> CaseRecord)
# ============================================================================

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
TOKEN_RE = re.compile(r"[a-ząćęłńóśźż0-9]{2,}", re.IGNORECASE)

# Poprawione regexy dla ContractPerformingNotice (obsługują zarówno '?' jak i ':')
RE_ON_TIME = re.compile(
    r"wykonana w pierwotn(?:ie określonym|ym) terminie[^:?]{0,40}[:?]\s*(Tak|Nie)", re.IGNORECASE
)
RE_CHANGED = re.compile(
    r"(?:Czy umowa została zmieniona|zmiany umowy)[^:?]{0,40}[:?]\s*(Tak|Nie)", re.IGNORECASE
)
RE_PROPER = re.compile(
    r"umowę wykonano należycie[^:?]{0,40}[:?]\s*(Tak|Nie)", re.IGNORECASE
)
RE_ART455 = re.compile(r"art\.\s*455\s*ust\.\s*(\d)(?:\s*pkt\s*(\d))?", re.IGNORECASE)
RE_CHANGE_REASON = re.compile(
    r"(?:Przyczyny dokonania zmian|Krótki opis zmiany|Opis zmiany)[^:?]{0,30}[:?]\s*(.{20,400}?)(?:\s*\d+\.\d+\.|\s*SEKCJA|\Z)",
    re.IGNORECASE,
)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", raw))).strip()


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

            title = rec.get("orderObject") or ""
            ntype = rec.get("noticeType") or ""
            nnum = rec.get("noticeNumber") or ""

            if tid not in cases:
                cases[tid] = CaseRecord(
                    tender_id=tid,
                    org_name=rec.get("organizationName") or "",
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

            if ntype == "ContractNotice":
                c.has_contract_notice = True
            elif ntype == "TenderResultNotice":
                c.has_result_notice = True
            elif ntype == "ContractPerformingNotice":
                c.has_performing_notice = True
                txt = strip_html(rec.get("htmlBody") or "")
                if m := RE_ON_TIME.search(txt):
                    c.executed_on_time = m.group(1).capitalize()
                if m := RE_CHANGED.search(txt):
                    c.was_modified = m.group(1).capitalize()
                if m := RE_PROPER.search(txt):
                    c.executed_properly = m.group(1).capitalize()
            elif ntype == "AgreementUpdateNotice":
                c.update_count += 1
                txt = strip_html(rec.get("htmlBody") or "")
                for m in RE_ART455.finditer(txt):
                    base = f"art. 455 ust. {m.group(1)}" + (f" pkt {m.group(2)}" if m.group(2) else "")
                    if base not in c.art455_bases:
                        c.art455_bases.append(base)
                if m := RE_CHANGE_REASON.search(txt):
                    c.change_quotes.append({"noticeNumber": nnum, "quote": m.group(1).strip()})

    # Zostawiamy tylko sprawy z zakończoną realizacją lub aneksami (zarówno sukcesy, jak i problemy!)
    return [c for c in cases.values() if c.has_performing_notice or c.update_count > 0]


# ============================================================================
# 2. MODUŁ BAZY DANYCH (SQLite — łatwo podmienić na PostgreSQL + pgvector)
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
                org_name TEXT,
                org_nip TEXT,
                province TEXT,
                title TEXT,
                subsector TEXT,
                year TEXT,
                has_performing INTEGER,
                update_count INTEGER,
                executed_on_time TEXT,
                was_modified TEXT,
                executed_properly TEXT,
                art455_json TEXT,
                quotes_json TEXT,
                notices_json TEXT
            )
        """)
        self.conn.commit()

    def save_cases(self, records: list[CaseRecord]) -> None:
        rows = [
            (
                c.tender_id, c.org_name, c.org_nip, c.province, c.title, c.subsector, c.year,
                int(c.has_performing_notice), c.update_count, c.executed_on_time,
                c.was_modified, c.executed_properly,
                json.dumps(c.art455_bases, ensure_ascii=False),
                json.dumps(c.change_quotes, ensure_ascii=False),
                json.dumps(c.notice_numbers, ensure_ascii=False),
            )
            for c in records
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        self.conn.commit()

    def load_all(self) -> list[CaseRecord]:
        cur = self.conn.execute("SELECT * FROM cases")
        out = []
        for r in cur.fetchall():
            out.append(CaseRecord(
                tender_id=r["tender_id"],
                org_name=r["org_name"],
                org_nip=r["org_nip"],
                province=r["province"],
                title=r["title"],
                subsector=r["subsector"],
                year=r["year"],
                has_performing_notice=bool(r["has_performing"]),
                update_count=r["update_count"],
                executed_on_time=r["executed_on_time"],
                was_modified=r["was_modified"],
                executed_properly=r["executed_properly"],
                art455_bases=json.loads(r["art455_json"]),
                change_quotes=json.loads(r["quotes_json"]),
                notice_numbers=json.loads(r["notices_json"]),
            ))
        return out


# ============================================================================
# 3. MODUŁ NL -> SOFT FILTERS (PODMIENIALNY NA LLM)
# ============================================================================

class BaseQueryParser(ABC):
    @abstractmethod
    def parse(self, user_query: str) -> SoftQuery: ...


class HeuristicQueryParser(BaseQueryParser):
    """Działa offline bez API — idealny baseline do testów. Podmień na LLMQueryParser."""
    PROV_MAP = {
        "dolnośląsk": "PL02", "kujawsko": "PL04", "lubelsk": "PL06", "lubusk": "PL08",
        "łódzk": "PL10", "małopolsk": "PL12", "mazowieck": "PL14", "opolsk": "PL16",
        "podkarpack": "PL18", "podlask": "PL20", "pomorsk": "PL22", "śląsk": "PL24",
        "świętokrzysk": "PL26", "warmińsko": "PL28", "wielkopolsk": "PL30", "zachodniopomorsk": "PL32",
    }
    HINT_MAP = {
        "grunty": ("działk", "prywatn", "właściciel", "zgody", "teren"),
        "kolizje": ("kolizj", "uzbrojeni", "przekroczeni", "ciek", "kolej", "drog"),
        "zaprojektuj": ("zaprojektuj", "pfu", "formule", "projektuj i buduj"),
        "srodowisko": ("środowisk", "natura 2000", "wodnoprawn"),
    }

    def parse(self, user_query: str) -> SoftQuery:
        q = user_query.lower()
        sub = infer_subsector(q)
        prov = next((code for k, code in self.PROV_MAP.items() if k in q), None)
        hints = [k for k, words in self.HINT_MAP.items() if any(w in q for w in words)]
        return SoftQuery(
            raw_text=user_query,
            subsector=sub if sub != "inne_wodkan" else None,
            province=prov,
            risk_hints=hints,
        )


# ============================================================================
# 4. MODUŁ EMBEDDINGÓW I HYBRYDOWEGO WYSZUKIWANIA (BM25 + VEC + SOFT + RRF)
# ============================================================================

class BaseEmbedder(ABC):
    @abstractmethod
    def embed(self, text: str) -> dict[str, float]: ...


class CharNgramEmbedder(BaseEmbedder):
    """
    Lekki, offline'owy wektor znakowych 4-gramów (łapie polską fleksję: kanalizacja/kanalizacyjnej).
    W produkcji podmień tę klasę na OpenAI / SentenceTransformers / pgvector!
    """
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

    def _soft_filter_score(self, case: CaseRecord, sq: SoftQuery) -> tuple[float, list[str]]:
        """Miękkie filtry: dodają punkty za dopasowanie, nigdy nie wyrzucają rekordu."""
        score = 0.0
        reasons = []
        if sq.subsector and case.subsector == sq.subsector:
            score += 2.0
            reasons.append(f"podsektor={case.subsector}")
        if sq.province and case.province == sq.province:
            score += 0.8
            reasons.append(f"woj={case.province}")
        txt = case.searchable_text.lower()
        for hint in sq.risk_hints:
            if any(w in txt for w in HeuristicQueryParser.HINT_MAP.get(hint, ())):
                score += 1.2
                reasons.append(f"sygnał={hint}")
        return score, reasons

    def search(self, sq: SoftQuery, top_k: int = 10, rrf_k: int = 60) -> list[dict[str, Any]]:
        bm25 = self._bm25_scores(sq.raw_text)
        q_vec = self.embedder.embed(sq.raw_text)
        vec = [cosine_sparse(q_vec, v) for v in self.vectors]
        soft_res = [self._soft_filter_score(c, sq) for c in self.cases]
        soft = [s[0] for s in soft_res]

        def to_ranks(vals: list[float]) -> dict[int, int]:
            order = sorted(range(len(vals)), key=lambda idx: vals[idx], reverse=True)
            return {idx: rank + 1 for rank, idx in enumerate(order)}

        r_bm25 = to_ranks(bm25)
        r_vec = to_ranks(vec)
        r_soft = to_ranks(soft)

        # Reciprocal Rank Fusion (ważone: BM25 + Wektor + Miękkie filtry)
        fused = []
        for idx, case in enumerate(self.cases):
            rrf = (
                1.0 / (rrf_k + r_bm25[idx])
                + 1.0 / (rrf_k + r_vec[idx])
                + 0.8 / (rrf_k + r_soft[idx])
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
# 5. MODUŁ ANALIZY RYZYK I GENEROWANIA RAPORTU (Z DOWODEM ŹRÓDŁOWYM)
# ============================================================================

def print_risk_report(sq: SoftQuery, results: list[dict[str, Any]]) -> None:
    n = len(results)
    with_updates = [r["case"] for r in results if r["case"].update_count > 0]
    delayed = [r["case"] for r in results if r["case"].executed_on_time == "Nie"]
    successes = [
        r["case"] for r in results
        if r["case"].update_count == 0 and r["case"].executed_on_time != "Nie"
    ]
    art_counts = Counter(b for r in results for b in r["case"].art455_bases)

    print("\n" + "=" * 78)
    print(f"ZAPYTANIE: {sq.raw_text}")
    print(f"MIĘKKIE FILTRY: podsektor={sq.subsector}, woj={sq.province}, sygnały={sq.risk_hints}")
    print("=" * 78)
    print(f"GRUPA PORÓWNAWCZA (N={n}):")
    print(f"  • Inwestycje z aneksami (AgreementUpdateNotice): {len(with_updates)}/{n} ({100*len(with_updates)/n:.0f}%)")
    print(f"  • Potwierdzone opóźnienia (po terminie):         {len(delayed)}/{n}")
    print(f"  • Inwestycje bez wykrytych aneksów (sukcesy):    {len(successes)}/{n}")
    if art_counts:
        print(f"  • Najczęstsze podstawy zmian:                    {dict(art_counts.most_common(4))}")

    print("\nTOP PODOBNE INWESTYCJE (z wyjaśnieniem podobieństwa i cytatami źródłowymi):")
    for i, r in enumerate(results, 1):
        c: CaseRecord = r["case"]
        ex = r["explain"]
        status = f"aneksów={c.update_count}, w_terminie={c.executed_on_time}"
        print(f"\n[{i}] {c.title[:100]}")
        print(f"    Zamawiający: {c.org_name} (NIP: {c.org_nip}, {c.province}, {c.year}) | {status}")
        print(f"    Podobieństwo (RRF={r['rrf_score']}): BM25=#{ex['bm25_rank']}, Vec=#{ex['vec_rank']}, Soft={ex['soft_matches']}")
        for q in c.change_quotes[:2]:
            print(f"    -> DOWÓD [{q['noticeNumber']}]: \"{q['quote'][:220]}\"")

    if successes:
        print("\nDO KOGO SIĘ ODEZWAĆ (podobne inwestycje bez aneksów):")
        for c in successes[:3]:
            print(f"  • {c.org_name} ({c.province}) — \"{c.title[:80]}\"")


# ============================================================================
# CLI
# ============================================================================

def inspect_performing(jsonl_path: Path) -> None:
    """Diagnostyka: wypisuje fragment htmlBody z 2 ogłoszeń ContractPerformingNotice."""
    found = 0
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("noticeType") == "ContractPerformingNotice":
                txt = strip_html(rec.get("htmlBody") or "")
                idx = txt.lower().find("przebieg realizacji")
                snippet = txt[max(0, idx): idx + 900] if idx != -1 else txt[:900]
                print(f"\n--- {rec.get('noticeNumber')} ---\n{snippet}")
                found += 1
                if found >= 2:
                    break


def main() -> None:
    ap = argparse.ArgumentParser(description="Risk Miner — modularny prototyp PoC")
    ap.add_argument("--jsonl", type=Path, default=Path("bzp_kopia/notices.jsonl"))
    ap.add_argument("--db", type=Path, default=Path("risk_miner_poc.sqlite"))
    ap.add_argument("--ingest", action="store_true", help="Zbuduj/odśwież bazę SQLite z JSONL")
    ap.add_argument("--limit", type=int, default=None, help="Limit linii JSONL do szybkiego testu")
    ap.add_argument("--query", type=str, help="Zapytanie w języku naturalnym")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--inspect-performing", action="store_true", help="Pokaż surowy tekst sekcji V")
    args = ap.parse_args()

    if args.inspect_performing:
        inspect_performing(args.jsonl)
        return

    store = CaseStore(args.db)
    if args.ingest or not args.db.exists():
        print(f"Indeksuję {args.jsonl} (limit={args.limit})...")
        records = parse_bzp_jsonl(args.jsonl, max_notices=args.limit)
        store.save_cases(records)
        on_time_stats = Counter(r.executed_on_time for r in records if r.has_performing_notice)
        print(f"Zapisano {len(records):,} spraw do {args.db}. Statystyki terminowości: {dict(on_time_stats)}")

    if args.query:
        cases = store.load_all()
        if not cases:
            raise SystemExit("Baza jest pusta. Uruchom najpierw z flagą --ingest.")
        parser = HeuristicQueryParser()
        sq = parser.parse(args.query)
        retriever = HybridRetriever(cases, embedder=CharNgramEmbedder())
        results = retriever.search(sq, top_k=args.top_k)
        print_risk_report(sq, results)


if __name__ == "__main__":
    main()
