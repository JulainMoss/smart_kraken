#!/usr/bin/env python3
"""
Risk Miner — Multi-Source Harvester (KIO/SAOS API + KPOŚK/dane.gov.pl API + NIK + RIO + BK2021).
Wszystkie pobrane zasoby trafiają do wspólnego data_lake/ z manifestem SHA-256.
Wymaga wyłącznie biblioteki standardowej Pythona 3.10+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

USER_AGENT = "Mozilla/5.0 (compatible; RiskMinerHarvester/2.0; HackNation2026)"
WS_RE = re.compile(r"\s+")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 25,
    retries: int = 3,
) -> tuple[int, bytes, dict[str, str]]:
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2.0 ** attempt)
                continue
            return e.code, body, dict(e.headers.items())
        except Exception:
            if attempt < retries:
                time.sleep(2.0 ** attempt)
                continue
            raise
    return 0, b"", {}


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._href = a["href"]
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, WS_RE.sub(" ", "".join(self._buf)).strip()))
            self._href = None
            self._buf = []


# ============================================================================
# WSPÓLNY DATA LAKE (ZGODNY Z harvest_external_dbs.py)
# ============================================================================

class DataLake:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / "manifest.jsonl"
        self.seen: set[str] = set()
        if self.manifest.exists():
            for ln in self.manifest.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                    if rec.get("http_status") == 200 and rec.get("source_url"):
                        self.seen.add(rec["source_url"])
                except json.JSONDecodeError:
                    pass

    def already_fetched(self, url: str) -> bool:
        return url in self.seen

    def log(
        self,
        source: str,
        ext_id: str,
        url: str,
        local_path: Path | None,
        status: int,
        sha256: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "source_system": source,
            "external_id": ext_id,
            "source_url": url,
            "local_path": str(local_path.relative_to(self.root)) if local_path else None,
            "http_status": status,
            "sha256": sha256,
            "fetched_at": now_iso(),
            "meta": meta or {},
        }
        with self.manifest.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if status == 200:
            self.seen.add(url)

    def save_bytes(
        self,
        subdir: str,
        filename: str,
        content: bytes,
        source: str,
        ext_id: str,
        url: str,
        meta: dict[str, Any] | None = None,
    ) -> Path:
        d = self.root / subdir
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename)[:140]
        p = d / safe
        p.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        self.log(source, ext_id, url, p, 200, sha, meta)
        return p


# ============================================================================
# 1. KIO — OFICJALNE REST API SAOS (NATIONAL_APPEAL_CHAMBER)
# ============================================================================

def harvest_kio_saos(
    lake: DataLake,
    queries: list[str],
    max_per_query: int = 20,
    delay: float = 0.4,
) -> None:
    """
    Pobiera pełne treści orzeczeń Krajowej Izby Odwoławczej przez otwarte REST API SAOS:
      - wyszukiwanie: GET https://www.saos.org.pl/api/search/judgments
      - pełna treść:  GET https://www.saos.org.pl/api/judgments/{id}
    """
    print(f"\n[KIO/SAOS] Pobieram orzeczenia KIO dla fraz: {queries} (limit/fraza={max_per_query})...", flush=True)
    out_dir = lake.root / "kio"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "kio_all_investments.jsonl"

    seen_ids: set[int] = set()
    if out_file.exists():
        for ln in out_file.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                try:
                    seen_ids.add(int(json.loads(ln)["id"]))
                except Exception:
                    pass

    added = 0
    with out_file.open("a", encoding="utf-8") as dst:
        for q in queries:
            qs = urllib.parse.urlencode({
                "courtType": "NATIONAL_APPEAL_CHAMBER",
                "all": q,
                "pageSize": min(100, max_per_query),
                "pageNumber": 0,
                "sortingField": "JUDGMENT_DATE",
                "sortingDirection": "DESC",
            })
            search_url = f"https://www.saos.org.pl/api/search/judgments?{qs}"
            st, body, _ = http_get(search_url, headers={"Accept": "application/json"})
            if st != 200:
                print(f"  [KIO] Błąd HTTP {st} dla '{q}'")
                continue

            items = json.loads(body.decode("utf-8")).get("items", [])
            print(f"  [KIO] Fraza '{q}': znaleziono {len(items)} orzeczeń.", flush=True)
            for it in items:
                jid = it.get("id")
                if not jid or jid in seen_ids:
                    continue
                detail_url = f"https://www.saos.org.pl/api/judgments/{jid}"
                st_d, body_d, _ = http_get(detail_url, headers={"Accept": "application/json"})
                if st_d != 200:
                    continue
                detail = json.loads(body_d.decode("utf-8")).get("data", {})
                # Czyścimy HTML z textContent do czystego tekstu gotowego pod RAG/LLM
                raw_text = detail.get("textContent") or ""
                clean_text = WS_RE.sub(" ", html.unescape(re.sub(r"<[^>]+>", " ", raw_text))).strip()
                record = {
                    "id": jid,
                    "case_numbers": [c.get("caseNumber") for c in detail.get("courtCases", [])],
                    "judgment_date": detail.get("judgmentDate"),
                    "judgment_type": detail.get("judgmentType"),
                    "keywords": detail.get("keywords", []),
                    "source_url": f"https://www.saos.org.pl/judgments/{jid}",
                    "query_matched": q,
                    "text_length": len(clean_text),
                    "text_content": clean_text,
                    "fetched_at": now_iso(),
                }
                dst.write(json.dumps(record, ensure_ascii=False) + "\n")
                seen_ids.add(jid)
                added += 1
                time.sleep(delay)

    if out_file.exists() and out_file.stat().st_size > 0:
        sha = hashlib.sha256(out_file.read_bytes()).hexdigest()
        lake.log("KIO_SAOS", "kio_all_investments", "https://www.saos.org.pl/api/search/judgments", out_file, 200, sha, {"total": len(seen_ids)})
    print(f"[KIO/SAOS] Gotowe. Nowych orzeczeń: {added} (łącznie w {out_file}: {len(seen_ids)}).")


# ============================================================================
# 2. KPOŚK / AKPOŚK — OFICJALNE API OTWARTYCH DANYCH (api.dane.gov.pl/1.4)
# ============================================================================

def harvest_kposk_danegov(
    lake: DataLake,
    queries: list[str],
    max_files: int = 12,
    delay: float = 0.4,
) -> None:
    """
    Odkrywa i pobiera pliki KPOŚK / aglomeracji wod-kan przez oficjalne JSON:API dane.gov.pl.
    """
    print(f"\n[KPOŚK/dane.gov.pl] Szukam oficjalnych zbiorów dla fraz: {queries} (limit plików={max_files})...", flush=True)
    downloaded = 0
    for q in queries:
        if downloaded >= max_files:
            break
        qs = urllib.parse.urlencode({"q": q, "per_page": 10})
        url = f"https://api.dane.gov.pl/1.4/datasets?{qs}"
        st, body, _ = http_get(url, headers={"Accept": "application/json"})
        if st != 200:
            print(f"  [dane.gov.pl] HTTP {st} dla '{q}'")
            continue
        datasets = json.loads(body.decode("utf-8")).get("data", [])
        print(f"  [dane.gov.pl] Fraza '{q}': {len(datasets)} zbiorów danych.", flush=True)

        for ds in datasets:
            if downloaded >= max_files:
                break
            ds_id = ds.get("id", "unknown")
            ds_title = (ds.get("attributes") or {}).get("title", "")
            rel_url = ((ds.get("relationships") or {}).get("resources") or {}).get("links", {}).get("related")
            if not rel_url:
                continue
            st_r, body_r, _ = http_get(rel_url, headers={"Accept": "application/json"})
            if st_r != 200:
                continue
            resources = json.loads(body_r.decode("utf-8")).get("data", [])
            for res in resources[:2]:
                if downloaded >= max_files:
                    break
                attr = res.get("attributes") or {}
                fmt = (attr.get("format") or "").lower()
                dl_url = attr.get("download_url") or attr.get("file_url")
                if not dl_url or fmt not in ("csv", "xlsx", "xls", "json", "geojson", "xml"):
                    continue
                if lake.already_fetched(dl_url):
                    continue
                st_f, content, _ = http_get(dl_url)
                if st_f == 200 and len(content) > 200:
                    fname = f"danegov_{ds_id}_{res.get('id', 'r')}.{fmt}"
                    saved = lake.save_bytes(
                        "kposk", fname, content, "DANE_GOV_KPOSK", str(res.get("id")), dl_url,
                        {"dataset_title": ds_title, "resource_title": attr.get("title"), "format": fmt},
                    )
                    downloaded += 1
                    print(f"  [KPOŚK {downloaded}/{max_files}] {saved.name} ({len(content)//1024} KB) <- {ds_title[:65]}", flush=True)
                time.sleep(delay)
    print(f"[KPOŚK/dane.gov.pl] Gotowe. Pobrano {downloaded} plików.")


# ============================================================================
# 3. NIK — KANAŁ RSS + SCRAPER KONTROLI I PDF-ÓW (BRAK OFICJALNEGO REST API)
# ============================================================================

def harvest_nik_hybrid(lake: DataLake, max_pdfs: int = 12, delay: float = 0.6) -> None:
    """
    NIK nie udostępnia oficjalnego REST API. Ten moduł:
    1) Pobiera najnowsze wyniki kontroli z RSS / strony wyników kontroli,
    2) Filtruje po słowach kluczowych dot. inwestycji / wod-kan / KPO / zamówień,
    3) Pobiera załączone pliki PDF (informacje o wynikach i wystąpienia pokontrolne).
    """
    print(f"\n[NIK] Pobieram kontrole inwestycyjne z RSS + HTML (limit PDF={max_pdfs})...", flush=True)
    base = "https://www.nik.gov.pl"
    candidates: dict[str, str] = {}  # url -> title
    kw_re = re.compile(r"(inwestycj|wodociąg|kanalizac|ściek|kpo|odbudow|zamówień|budow|infrastruktur)", re.I)

    # Źródło A: kanał RSS wyników kontroli (stabilny XML)
    for rss_url in (
        f"{base}/kontrole/wyniki-kontroli-nik/rss.xml",
        f"{base}/rss/wyniki-kontroli.xml",
    ):
        st, body, _ = http_get(rss_url)
        if st == 200 and b"<item>" in body:
            xml_txt = body.decode("utf-8", errors="ignore")
            for item in re.findall(r"<item>(.*?)</item>", xml_txt, re.S):
                t_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
                l_m = re.search(r"<link>(.*?)</link>", item, re.S)
                if t_m and l_m:
                    title = WS_RE.sub(" ", html.unescape(t_m.group(1))).strip()
                    link = l_m.group(1).strip()
                    if kw_re.search(title):
                        candidates[link] = title

    # Źródło B: strona wyników kontroli (GET)
    st, body, _ = http_get(f"{base}/kontrole/wyniki-kontroli-nik/")
    if st == 200:
        lp = _LinkParser()
        lp.feed(body.decode("utf-8", errors="ignore"))
        for href, title in lp.links:
            if len(title) >= 15 and kw_re.search(title) and ("kontrol" in href.lower() or "/p/" in href.lower() or "/i/" in href.lower()):
                candidates[urllib.parse.urljoin(base, href)] = title

    print(f"  [NIK] Wytypowano {len(candidates)} stron kontroli pasujących tematycznie.", flush=True)
    if not candidates:
        print("  [NIK] Brak trafień na stronie głównej — wklej konkretne linki PDF NIK do bip_urls.txt.")
        return

    pdf_cnt = 0
    for audit_url, title in candidates.items():
        if pdf_cnt >= max_pdfs:
            break
        st_a, body_a, _ = http_get(audit_url)
        if st_a != 200:
            continue
        lp = _LinkParser()
        lp.feed(body_a.decode("utf-8", errors="ignore"))
        pdf_urls = [
            urllib.parse.urljoin(base, h)
            for h, _ in lp.links
            if ".pdf" in h.lower() or "/plik/id," in h.lower()
        ]
        for purl in dict.fromkeys(pdf_urls):
            if pdf_cnt >= max_pdfs:
                break
            if lake.already_fetched(purl):
                continue
            st_p, content, hdrs = http_get(purl)
            ctype = (hdrs.get("Content-Type") or hdrs.get("content-type") or "").lower()
            if st_p == 200 and (content.startswith(b"%PDF") or "pdf" in ctype) and len(content) > 5_000:
                h = hashlib.sha256(purl.encode()).hexdigest()[:8]
                saved = lake.save_bytes(
                    "nik/pdfs", f"nik_{h}.pdf", content, "NIK", h, purl, {"audit_title": title, "audit_url": audit_url}
                )
                pdf_cnt += 1
                print(f"  [NIK PDF {pdf_cnt}/{max_pdfs}] {saved.name} ({len(content)//1024} KB) <- {title[:60]}", flush=True)
            time.sleep(delay)
    print(f"[NIK] Gotowe. Pobrano {pdf_cnt} plików PDF.")


# ============================================================================
# 4. RIO — SCRAPER WYSTĄPIEŃ POKONTROLNYCH (NA BAZIE REALNEJ STRUKTURY RIO WARSZAWA)
# ============================================================================

def harvest_rio_warszawa(lake: DataLake, max_pdfs: int = 10, delay: float = 0.6) -> None:
    """
    Pobiera wystąpienia pokontrolne z aktualnego BIP RIO Warszawa (2026 r.)
    — struktura zweryfikowana: /kategorie/226-2026-r -> /artykuly/... -> .pdf
    """
    cat_url = "https://bip.warszawa.rio.gov.pl/kategorie/226-2026-r?lang=PL"
    print(f"\n[RIO Warszawa] Skanuję wystąpienia pokontrolne 2026 (limit PDF={max_pdfs})...", flush=True)
    st, body, _ = http_get(cat_url)
    if st != 200:
        print(f"  [RIO] Błąd HTTP {st} przy pobieraniu {cat_url}")
        return

    lp = _LinkParser()
    lp.feed(body.decode("utf-8", errors="ignore"))
    article_urls = [
        (urllib.parse.urljoin(cat_url, h), t)
        for h, t in lp.links
        if "/artykuly/" in h and "wystapienie" in h.lower()
    ]
    print(f"  [RIO] Znaleziono {len(article_urls)} wystąpień pokontrolnych na liście.", flush=True)

    pdf_cnt = 0
    for art_url, art_title in article_urls:
        if pdf_cnt >= max_pdfs:
            break
        st_a, body_a, _ = http_get(art_url)
        if st_a != 200:
            continue
        lp2 = _LinkParser()
        lp2.feed(body_a.decode("utf-8", errors="ignore"))
        for h, _ in lp2.links:
            if ".pdf" not in h.lower() and "pobierz" not in h.lower() and "/zalaczniki/" not in h.lower():
                continue
            purl = urllib.parse.urljoin(art_url, h)
            if lake.already_fetched(purl):
                continue
            st_p, content, hdrs = http_get(purl)
            ctype = (hdrs.get("Content-Type") or hdrs.get("content-type") or "").lower()
            if st_p == 200 and (content.startswith(b"%PDF") or "pdf" in ctype) and len(content) > 5_000:
                h_id = hashlib.sha256(purl.encode()).hexdigest()[:8]
                saved = lake.save_bytes(
                    "rio/pdfs", f"rio_waw_{h_id}.pdf", content, "RIO_WARSZAWA", h_id, purl,
                    {"title": art_title, "article_url": art_url},
                )
                pdf_cnt += 1
                print(f"  [RIO PDF {pdf_cnt}/{max_pdfs}] {saved.name} ({len(content)//1024} KB) <- {art_title[:60]}", flush=True)
                break
        time.sleep(delay)
    print(f"[RIO Warszawa] Gotowe. Pobrano {pdf_cnt} plików PDF.")


# ============================================================================
# 5. BK2021 — BAZA KONKURENCYJNOŚCI (PRÓBA JSON API + DIAGNOSTYKA)
# ============================================================================

def harvest_bk2021(lake: DataLake, keyword: str = "kanalizac", limit: int = 20) -> None:
    """
    Odpytuje publiczny endpoint wyszukiwania Bazy Konkurencyjności 2021.
    """
    print(f"\n[BK2021] Odpytuję Bazę Konkurencyjności dla frazy '{keyword}' (limit={limit})...", flush=True)
    qs = urllib.parse.urlencode({"q": keyword, "page": 1, "limit": limit})
    api_url = f"https://bazakonkurencyjnosci.funduszeeuropejskie.gov.pl/api/announcements?{qs}"
    st, body, hdrs = http_get(api_url, headers={"Accept": "application/json"})
    ctype = (hdrs.get("Content-Type") or hdrs.get("content-type") or "").lower()
    if st == 200 and "json" in ctype:
        saved = lake.save_bytes("bk2021", f"bk2021_{keyword}.json", body, "BK2021", keyword, api_url)
        print(f"  [BK2021] Zapisano JSON: {saved} ({len(body)//1024} KB).")
    else:
        print(
            f"  [BK2021] Endpoint zwrócił HTTP {st} ({ctype}). "
            "BK2021 to aplikacja SPA — jeśli zmieniła ścieżkę API, podejrzyj XHR w DevTools (zakładka Network) "
            "i podmień `api_url` w harvest_bk2021()."
        )


# ============================================================================
# 6. BZP LIVE MULTISECTOR (PRODUKCYJNE API e-ZAMÓWIENIA — POZA WOD-KAN)
# ============================================================================

BZP_LIVE_SECTORS: dict[str, str] = {
    "drogi_i_mosty": "45233",
    "budownictwo_kubaturowe": "4521",
    "energetyka_oze": "452314",
    "kolej_transport": "45234",
}


def harvest_bzp_live_multisector(lake: DataLake, page_size: int = 80, delay: float = 2.5) -> None:
    """
    Pobiera próbki wielosektorowych ogłoszeń o zmianach i wykonaniu umów z produkcyjnego API BZP
    (kopia lokalna ma tylko wod-kan). Bezpieczny delay >= 2.5s szanuje limit 30 req/min.
    """
    print(f"\n[BZP LIVE] Pobieram wielosektorowe aneksy i wykonania z ezamowienia.gov.pl (delay={delay}s)...", flush=True)
    out_dir = lake.root / "bzp_multisector"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "bzp_other_sectors.jsonl"

    seen_ids: set[str] = set()
    if out_file.exists():
        for ln in out_file.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                try:
                    seen_ids.add(json.loads(ln).get("objectId", ""))
                except Exception:
                    pass

    added = 0
    with out_file.open("a", encoding="utf-8") as dst:
        for sector, cpv_prefix in BZP_LIVE_SECTORS.items():
            for ntype in ("AgreementUpdateNotice", "ContractPerformingNotice"):
                qs = urllib.parse.urlencode({
                    "NoticeType": ntype,
                    "OrderType": "Works",
                    "CpvCode": cpv_prefix,
                    "PublicationDateFrom": "2024-01-01T00:00:00",
                    "PublicationDateTo": "2026-11-21T23:59:59",
                    "PageSize": page_size,
                })
                url = f"https://ezamowienia.gov.pl/mo-board/api/v1/notice?{qs}"
                st, body, _ = http_get(url, headers={"Accept": "application/json"})
                if st in (403, 429):
                    print(f"  [BZP LIVE] Limit zapytań / blokada IP (HTTP {st}) — przerywam bezpiecznie.")
                    return
                if st != 200:
                    print(f"  [BZP LIVE:{sector}/{ntype}] HTTP {st}")
                    time.sleep(delay)
                    continue
                items = json.loads(body.decode("utf-8"))
                new_here = 0
                for rec in items:
                    oid = rec.get("objectId")
                    if not oid or oid in seen_ids:
                        continue
                    seen_ids.add(oid)
                    rec["_sector"] = sector
                    dst.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    new_here += 1
                    added += 1
                print(f"  [BZP LIVE:{sector}/{ntype}] +{new_here} nowych (CPV {cpv_prefix}*)", flush=True)
                time.sleep(delay)

    if out_file.exists() and out_file.stat().st_size > 0:
        sha = hashlib.sha256(out_file.read_bytes()).hexdigest()
        lake.log("BZP_LIVE_MULTISECTOR", "bzp_other_sectors", "https://ezamowienia.gov.pl/mo-board/api/v1/notice",
                 out_file, 200, sha, {"total": len(seen_ids)})
    print(f"[BZP LIVE] Gotowe. Nowych: {added}, łącznie w {out_file}: {len(seen_ids)} ogłoszeń.")


# ============================================================================
# CLI
# ============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Risk Miner — Multi-Sector Harvester")
    ap.add_argument("--out", type=Path, default=Path("data_lake"))
    ap.add_argument("--all", action="store_true", help="Uruchom wszystkie źródła wielosektorowe")
    ap.add_argument("--kio", action="store_true", help="Orzeczenia KIO (SAOS) — wszystkie sektory inwestycji")
    ap.add_argument("--kposk", action="store_true", help="Zbiory inwestycyjne z api.dane.gov.pl")
    ap.add_argument("--nik", action="store_true", help="Raporty PDF kontroli inwestycyjnych NIK")
    ap.add_argument("--rio", action="store_true", help="Wystąpienia pokontrolne PDF z RIO Warszawa")
    ap.add_argument("--bk2021", action="store_true", help="Ogłoszenia inwestycyjne z BK2021")
    ap.add_argument("--bzp-live", action="store_true", help="Wielosektorowe BZP z ezamowienia.gov.pl (limit-safe)")
    args = ap.parse_args()

    if not any((args.all, args.kio, args.kposk, args.nik, args.rio, args.bk2021, args.bzp_live)):
        ap.print_help()
        return

    lake = DataLake(args.out)

    if args.all or args.kio:
        harvest_kio_saos(
            lake,
            queries=[
                "roboty budowlane droga most",
                "budowa szkoły szpitala kubaturowe",
                "termomodernizacja fotowoltaika energetyka",
                "modernizacja linii kolejowej",
                "kanalizacja sanitarna sieć wodociągowa",
            ],
            max_per_query=12,
        )
    if args.all or args.kposk:
        harvest_kposk_danegov(
            lake,
            queries=["inwestycje publiczne", "KPO inwestycje", "inwestycje drogowe", "KPOŚK"],
            max_files=12,
        )
    if args.all or args.nik:
        harvest_nik_hybrid(lake, max_pdfs=15)
    if args.all or args.rio:
        harvest_rio_warszawa(lake, max_pdfs=12)
    if args.all or args.bk2021:
        for kw in ("roboty budowlane", "termomodernizacja", "kanalizacja"):
            harvest_bk2021(lake, keyword=kw, limit=20)
    if args.all or args.bzp_live:
        harvest_bzp_live_multisector(lake, page_size=80, delay=2.5)

    print(f"\n=== GOTOWE. Sprawdź zawartość: ls -lhR {args.out} ===")


if __name__ == "__main__":
    main()
