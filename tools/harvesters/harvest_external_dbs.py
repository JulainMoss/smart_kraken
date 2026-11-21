#!/usr/bin/env python3
"""
Risk Miner — Offline Data Lake Harvester (TED API v3 + NIK PDFs + Custom BIP/RIO URLs).
Idempotentny, wznawialny i odporny na błędy sieciowe (backoff + manifest SHA-256).
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
from pathlib import Path
from typing import Any

USER_AGENT = "Mozilla/5.0 (compatible; RiskMinerHarvester/1.0; HackNation2026)"
WS_RE = re.compile(r"\s+")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def http_request(
    url: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 25,
    retries: int = 3,
) -> tuple[int, bytes, dict[str, str]]:
    """Zapytanie HTTP z wykładniczym backoffem dla 429/5xx."""
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdrs)
    last_err: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = 2.0 ** attempt
                print(f"    [HTTP {e.code}] Retry {attempt}/{retries} za {wait:.0f}s: {url}", flush=True)
                time.sleep(wait)
                last_err = e
                continue
            return e.code, body, dict(e.headers.items())
        except Exception as e:
            if attempt < retries:
                time.sleep(2.0 ** attempt)
                last_err = e
                continue
            raise
    raise RuntimeError(f"Nie udało się pobrać {url}: {last_err}")


# ============================================================================
# 1. ZARZĄDCA DATA LAKE (MANIFEST + DEDUPLIKACJA + SHA-256)
# ============================================================================

class DataLake:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.jsonl"
        self.seen_urls: set[str] = set()
        self._load_manifest()

    def _load_manifest(self) -> None:
        if not self.manifest_path.exists():
            return
        with self.manifest_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("http_status") == 200 and rec.get("source_url"):
                        self.seen_urls.add(rec["source_url"])
                except json.JSONDecodeError:
                    continue

    def already_fetched(self, url: str) -> bool:
        return url in self.seen_urls

    def record(
        self,
        source_system: str,
        external_id: str,
        source_url: str,
        local_path: Path | None,
        http_status: int,
        sha256: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "source_system": source_system,
            "external_id": external_id,
            "source_url": source_url,
            "local_path": str(local_path.relative_to(self.root)) if local_path else None,
            "http_status": http_status,
            "sha256": sha256,
            "fetched_at": now_iso(),
            "meta": meta or {},
        }
        with self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if http_status == 200:
            self.seen_urls.add(source_url)

    def save_binary(
        self,
        subdir: str,
        filename: str,
        content: bytes,
        source_system: str,
        external_id: str,
        source_url: str,
        meta: dict[str, Any] | None = None,
    ) -> Path:
        target_dir = self.root / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename)[:140]
        path = target_dir / safe_name
        path.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        self.record(source_system, external_id, source_url, path, 200, sha, meta)
        return path


# ============================================================================
# 2. HARVESTER TED API v3 (UNIJNE PRZETARGI WOD-KAN Z POLSKI)
# ============================================================================

def harvest_ted(lake: DataLake, max_notices: int = 200, delay: float = 0.5) -> None:
    print(f"\n[TED] Pobieram ogłoszenia wod-kan z Polski (limit={max_notices})...", flush=True)
    url = "https://api.ted.europa.eu/v3/notices/search"
    out_dir = lake.root / "ted"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "ted_wodkan_pl.jsonl"

    # Kody CPV dla sektora wod-kan (sieci wodociągowe, kanalizacja, oczyszczalnie, SUW)
    query = (
        "buyer-country=POL AND ("
        "classification-cpv=45231300 OR "
        "classification-cpv=45232400 OR "
        "classification-cpv=45252100 OR "
        "classification-cpv=45232410"
        ") SORT BY publication-date DESC"
    )
    fields = ["publication-number", "notice-title", "buyer-name", "notice-type", "publication-date"]

    existing_ids: set[str] = set()
    if out_file.exists():
        with out_file.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    existing_ids.add(json.loads(line).get("publication-number", ""))

    fetched = 0
    page = 1
    page_size = min(100, max_notices)

    with out_file.open("a", encoding="utf-8") as dst:
        while fetched < max_notices:
            payload = json.dumps({
                "query": query,
                "fields": fields,
                "page": page,
                "limit": page_size,  # stały limit: offset = (page - 1) * limit
                "scope": "ALL",
            }).encode("utf-8")

            status, body, _ = http_request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            if status != 200:
                print(f"  [TED] Błąd HTTP {status}: {body.decode('utf-8', errors='ignore')[:300]}")
                break

            data = json.loads(body.decode("utf-8"))
            notices = data.get("notices", [])
            if not notices:
                break

            new_on_page = 0
            for n in notices:
                if fetched >= max_notices:
                    break
                pnum = n.get("publication-number", "")
                if not pnum or pnum in existing_ids:
                    continue
                existing_ids.add(pnum)
                n["_source_url"] = f"https://ted.europa.eu/en/notice/-/detail/{pnum}"
                n["_fetched_at"] = now_iso()
                dst.write(json.dumps(n, ensure_ascii=False) + "\n")
                new_on_page += 1
                fetched += 1

            print(f"  [TED] Strona {page}: pobrano {len(notices)} (nowych: {new_on_page}, łącznie nowych: {fetched})", flush=True)
            if len(notices) < page_size:
                break
            page += 1
            time.sleep(delay)

    if out_file.exists():
        sha = hashlib.sha256(out_file.read_bytes()).hexdigest()
        lake.record("TED", "ted_wodkan_pl", url, out_file, 200, sha, {"total_records": len(existing_ids)})
    print(f"[TED] Gotowe. Łącznie w {out_file}: {len(existing_ids)} ogłoszeń.")


# ============================================================================
# 3. HARVESTER NIK (KONTROLE INWESTYCYJNE + POBIERANIE PDF-ÓW)
# ============================================================================

def harvest_nik(
    lake: DataLake,
    keywords: list[str],
    max_audits_per_kw: int = 8,
    max_pdfs: int = 25,
    delay: float = 0.7,
) -> None:
    print(f"\n[NIK] Szukam kontroli dla fraz: {keywords} (limit PDF={max_pdfs})...", flush=True)
    base = "https://www.nik.gov.pl"
    search_url = f"{base}/kontrole/wyniki-kontroli-nik/"
    out_dir = lake.root / "nik"
    out_dir.mkdir(parents=True, exist_ok=True)
    index_file = out_dir / "audits_index.jsonl"

    audits: dict[str, str] = {}  # href -> title
    for kw in keywords:
        qs = urllib.parse.urlencode({"tytul": kw})
        status, body, _ = http_request(f"{search_url}?{qs}")
        if status != 200:
            print(f"  [NIK] Błąd HTTP {status} dla frazy '{kw}'")
            continue
        page_html = body.decode("utf-8", errors="ignore")
        matches = re.findall(
            r'href="(/kontrole/[A-Z]/\d{2}/\d{3}[^"]*)"[^>]*>([^<]{10,260})</a>', page_html
        )
        added = 0
        for href, raw_title in matches:
            if href not in audits:
                audits[href] = WS_RE.sub(" ", html.unescape(raw_title)).strip()
                added += 1
                if added >= max_audits_per_kw:
                    break
        print(f"  [NIK] Fraza '{kw}': znaleziono {added} kontroli.", flush=True)
        time.sleep(delay)

    # Zapisz indeks kontroli
    with index_file.open("w", encoding="utf-8") as f:
        for href, title in audits.items():
            f.write(json.dumps({"href": href, "url": base + href, "title": title}, ensure_ascii=False) + "\n")

    # Odwiedź strony kontroli i pobierz PDF-y (informacje o wynikach i wystąpienia pokontrolne)
    pdf_count = 0
    for href, title in audits.items():
        if pdf_count >= max_pdfs:
            break
        audit_url = base + href
        audit_id = href.strip("/").replace("/", "_")
        status, body, _ = http_request(audit_url)
        if status != 200:
            continue
        page_html = body.decode("utf-8", errors="ignore")

        # Linki do plików PDF na stronach NIK (/plik/id,... lub bezpośrednie *.pdf)
        pdf_links = re.findall(
            r'href="((?:https?://[^"]+)?(?:/plik/id,\d+[^"]*|[^"]+\.pdf))"', page_html, re.IGNORECASE
        )
        for raw_link in dict.fromkeys(pdf_links):
            if pdf_count >= max_pdfs:
                break
            full_url = raw_link if raw_link.startswith("http") else urllib.parse.urljoin(base, raw_link)
            if lake.already_fetched(full_url):
                continue
            st, content, hdrs = http_request(full_url)
            ctype = (hdrs.get("Content-Type") or hdrs.get("content-type") or "").lower()
            is_pdf = content.startswith(b"%PDF") or "pdf" in ctype
            if st == 200 and is_pdf and len(content) > 5_000:
                fname = f"{audit_id}_{pdf_count + 1}.pdf"
                saved = lake.save_binary(
                    "nik/pdfs", fname, content, "NIK", audit_id, full_url, {"audit_title": title}
                )
                pdf_count += 1
                print(f"  [NIK PDF {pdf_count}/{max_pdfs}] {saved.name} ({len(content)//1024} KB) <- {title[:60]}", flush=True)
            else:
                lake.record("NIK", audit_id, full_url, None, st, None, {"skipped_non_pdf": True})
            time.sleep(delay)

    print(f"[NIK] Gotowe. Pobrano {pdf_count} nowych plików PDF.")


# ============================================================================
# 4. HARVESTER DOWOLNYCH URL-I (BIP-Y GMIN, RIO, KPOŚK — DLA OSÓB NIETECHNICZNYCH)
# ============================================================================

def harvest_custom_urls(lake: DataLake, urls_file: Path, delay: float = 0.5) -> None:
    if not urls_file.exists():
        print(f"\n[URLS] Pominięto — brak pliku {urls_file}")
        return
    urls = [
        ln.strip() for ln in urls_file.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    print(f"\n[URLS] Archiwizuję {len(urls)} adresów z {urls_file}...", flush=True)
    ok = 0
    for i, url in enumerate(urls, 1):
        if lake.already_fetched(url):
            print(f"  [SKIP] Już w archiwum: {url}")
            continue
        try:
            st, content, hdrs = http_request(url)
            if st != 200:
                print(f"  [ERR {st}] {url}")
                lake.record("CUSTOM_BIP", f"url_{i}", url, None, st)
                continue
            ctype = (hdrs.get("Content-Type") or hdrs.get("content-type") or "").lower()
            ext = ".pdf" if (content.startswith(b"%PDF") or "pdf" in ctype) else (
                ".json" if "json" in ctype else ".html"
            )
            host = urllib.parse.urlparse(url).netloc.replace(".", "_")
            h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
            saved = lake.save_binary(
                "custom_bip/files", f"{host}_{h}{ext}", content, "CUSTOM_BIP", f"{host}_{h}", url
            )
            ok += 1
            print(f"  [OK] {saved.name} ({len(content)//1024} KB) <- {url}", flush=True)
        except Exception as e:
            print(f"  [ERR] {url}: {e}")
            lake.record("CUSTOM_BIP", f"url_{i}", url, None, 0, None, {"error": str(e)})
        time.sleep(delay)
    print(f"[URLS] Gotowe. Zarchiwizowano {ok} nowych zasobów.")


# ============================================================================
# CLI
# ============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Risk Miner — Offline Data Lake Harvester")
    ap.add_argument("--out", type=Path, default=Path("data_lake"), help="Katalog docelowy Data Lake")
    ap.add_argument("--all", action="store_true", help="Uruchom TED + NIK + custom URLs")
    ap.add_argument("--ted", action="store_true", help="Pobierz przetargi wod-kan z TED API v3")
    ap.add_argument("--max-ted", type=int, default=200)
    ap.add_argument("--nik", action="store_true", help="Pobierz raporty i PDF-y kontroli z NIK")
    ap.add_argument("--max-nik-pdfs", type=int, default=20)
    ap.add_argument("--urls", type=Path, default=Path("bip_urls.txt"), help="Plik z listą URL-i BIP/RIO/PDF")
    ap.add_argument("--delay", type=float, default=0.6, help="Opóźnienie między zapytaniami (sekundy)")
    args = ap.parse_args()

    if not (args.all or args.ted or args.nik or args.urls.exists()):
        ap.print_help()
        return

    lake = DataLake(args.out)

    if args.all or args.ted:
        harvest_ted(lake, max_notices=args.max_ted, delay=args.delay)

    if args.all or args.nik:
        harvest_nik(
            lake,
            keywords=["inwestycj", "ścieków", "Krajowego Planu Odbudowy"],
            max_audits_per_kw=6,
            max_pdfs=args.max_nik_pdfs,
            delay=args.delay,
        )

    if args.all or args.urls.exists():
        harvest_custom_urls(lake, args.urls, delay=args.delay)

    print(f"\n=== ZAKOŃCZONO. Manifest Data Lake: {lake.manifest_path} ===")


if __name__ == "__main__":
    main()