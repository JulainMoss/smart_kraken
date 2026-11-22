"""Źródła dodatkowe: TED (API v3), KIO (orzeczenia przez otwarte API SAOS), NIK (wyszukiwarka wyników kontroli),
BIP samorządów (generyczny robot przeszukujący BIP zamawiającego pod kątem dokumentów inwestycji).

Zasady: tylko publiczne dane, ograniczona liczba zapytań, odstęp między zapytaniami, rejestr każdej operacji."""

from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
import sqlite3
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from .. import config
from ..db import j, log_source
from ..docs import read_any
from ..textutil import fold, html_to_text, now_iso, zpack
from . import get_json, http


# ------------------------------------------------------------------------------------------------ TED
def ted_search(buyer_name: str, keywords: str = "", limit: int = 20) -> list[dict]:
    """Ogłoszenia TED zamawiającego z Polski (progi unijne). Zwraca listę rekordów TED."""
    url = config.get()["sources"]["ted_api"]
    name = re.sub(r'["\\]', " ", buyer_name)[:80]
    q = f'buyer-country=POL AND buyer-name~"{name}"'
    if keywords:
        q += f' AND FT~"{keywords}"'
    body = json.dumps({"query": q + " SORT BY publication-date DESC", "limit": limit, "scope": "ALL",
                       "fields": ["publication-number", "notice-title", "buyer-name", "notice-type", "publication-date",
                                  "classification-cpv", "total-value", "winner-name"]}).encode()
    st, data, _ = http(url, data=body, headers={"Content-Type": "application/json"}, timeout=30)
    if st != 200:
        return []
    return json.loads(data.decode("utf-8")).get("notices", [])


def ted_title(n: dict) -> str:
    t = n.get("notice-title") or {}
    if isinstance(t, dict):
        return (t.get("pol") or t.get("eng") or next(iter(t.values()), "")) if t else ""
    return str(t)


# ------------------------------------------------------------------------------------------------ KIO (SAOS)
def kio_search(phrase: str, limit: int = 10) -> list[dict]:
    base = config.get()["sources"]["saos_api"].rstrip("/")
    res = get_json(f"{base}/search/judgments", {"courtType": "NATIONAL_APPEAL_CHAMBER", "all": phrase, "pageSize": limit,
                                                 "sortingField": "JUDGMENT_DATE", "sortingDirection": "DESC"}, timeout=30)
    return res.get("items", [])


def kio_judgment(jid: int) -> dict:
    base = config.get()["sources"]["saos_api"].rstrip("/")
    return get_json(f"{base}/judgments/{jid}", timeout=30).get("data", {})


# ------------------------------------------------------------------------------------------------ BIP / NIK – robot WWW
class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            a = dict(attrs)
            if a.get("href"):
                self._href = a["href"]
                self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, re.sub(r"\s+", " ", "".join(self._buf)).strip()))
            self._href = None


NAV_WORDS = r"zamowien|przetarg|inwestycj|ogloszen|umow|rejestr|zarzadzen|uchwal|budzet|wpf|sprawozdan|aktualnosc|komunikat|kontrol|wystapien|szukaj|archiwum|rok|20\d\d"
DOC_EXT = (".pdf", ".docx", ".doc", ".odt", ".xlsx", ".xls", ".rtf")


def bip_candidates(www: str | None, email: str | None, org_name: str) -> list[str]:
    """Prawdopodobne adresy BIP zamawiającego (z ogłoszeń BZP: strona WWW i domena e-mail)."""
    out = []
    hosts = []
    for src in (www or "", (email or "").split("@")[-1] if email and "@" in email else ""):
        h = re.sub(r"^https?://", "", src.strip()).split("/")[0].lower()
        if h:
            hosts.append(h)
    for h in hosts:
        base = re.sub(r"^(www\.|bip\.)", "", h)
        for cand in (h if h.startswith("bip.") else None, f"bip.{base}", f"{base}/bip", f"www.{base}/bip", h):
            if cand and cand not in out:
                out.append(cand)
    return [("https://" + c) for c in out]


def page_is_hit(text: str, keywords: list[str]) -> bool:
    """To samo kryterium co w robocie – do ponownej oceny zapisanych dokumentów."""
    place = [fold(k) for k in keywords if k and k[:1].isupper() and len(k) >= 4]
    generic = [fold(k) for k in keywords if k and not k[:1].isupper() and len(k) >= 4]
    ft = fold(text)
    ph = sum(ft.count(k) for k in place)
    gh = sum(ft.count(k) for k in generic)
    return (ph >= 1 and gh >= 1) if (place and generic) else (ph >= 2 if place else gh >= 3)


def crawl_site(start_urls: list[str], keywords: list[str], max_pages: int = 40, delay: float | None = None,
               progress: Callable[[str], None] | None = None, search_paths: bool = True) -> list[dict]:
    """Przeszukuje serwis (BIP/NIK) od stron startowych, priorytetyzując linki z nawigacją zamówień/inwestycji
    i słowami kluczowymi inwestycji. Zwraca znalezione strony/dokumenty zawierające słowa kluczowe."""
    delay = config.get()["sources"]["request_delay_s"] if delay is None else delay
    kws = [fold(k) for k in keywords if k and len(k) >= 4]
    if not kws:
        return []
    # słowa specyficzne (nazwy miejscowości – wielka litera w oryginale) muszą wystąpić, żeby strona była trafieniem
    place = [fold(k) for k in keywords if k and k[:1].isupper() and len(k) >= 4]
    generic = [k for k in kws if k not in place]
    t_start = time.time()

    def score_text(ft: str) -> tuple[int, bool]:
        ph = sum(ft.count(k) for k in place)
        gh = sum(ft.count(k) for k in generic)
        ok = (ph >= 1 and gh >= 1) if (place and generic) else (ph >= 2 if place else gh >= 3)
        return ph * 3 + gh, ok
    seen: set[str] = set()
    queue: list[tuple[float, str, str]] = []
    domain = None
    for u in start_urls:
        queue.append((10.0, u, "start"))
    hits: list[dict] = []
    pages = 0
    while queue and pages < max_pages and time.time() - t_start < 150:
        queue.sort(key=lambda x: -x[0])
        prio, url, anchor = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            st, body, hd = http(url, timeout=15, retries=0, max_bytes=12_000_000)
        except Exception:
            continue
        pages += 1
        if st != 200 or not body:
            continue
        if domain is None:
            domain = urllib.parse.urlsplit(url).netloc
            if search_paths:
                # typowe wyszukiwarki BIP – zapytanie o najbardziej charakterystyczne słowo
                main_kw = keywords[0]
                for path in (f"/szukaj?q={urllib.parse.quote(main_kw)}", f"/wyszukiwarka?szukaj={urllib.parse.quote(main_kw)}",
                             f"/search?query={urllib.parse.quote(main_kw)}", f"/?s={urllib.parse.quote(main_kw)}"):
                    queue.append((9.0, f"{urllib.parse.urlsplit(url).scheme}://{domain}{path}", "wyszukiwarka"))
        ctype = hd.get("content-type", "")
        is_doc = url.lower().split("?")[0].endswith(DOC_EXT) or "pdf" in ctype or "officedocument" in ctype or "msword" in ctype
        if is_doc:
            try:
                name = url.split("?")[0].rsplit("/", 1)[-1] or "dokument.pdf"
                if "pdf" in ctype and not name.lower().endswith(".pdf"):
                    name += ".pdf"
                parsed = read_any(name, body)
                pages_txt = parsed["pages"]
            except Exception:
                continue
            text = "\n".join(t for _, t in pages_txt)
            score, ok = score_text(fold(text))
            if ok:
                hits.append({"url": url, "title": anchor or name, "kind": "dokument", "pages": pages_txt, "score": score,
                             "sha256": hashlib.sha256(body).hexdigest(), "mime": ctype, "bytes": len(body)})
            continue
        try:
            raw = body.decode("utf-8")
        except UnicodeDecodeError:
            raw = body.decode("cp1250", "replace")
        text = html_to_text(raw)
        ftext = fold(text)
        score, ok = score_text(ftext)
        listing = re.search(r"/tag/|[?&](s|q|query|szukaj|search)=|/szukaj|/wyszukiw|/search|/kategori", url.lower())
        if ok and anchor != "start" and len(text) > 200 and not listing:
            title = re.search(r"<title>(.*?)</title>", raw, re.S | re.I)
            hits.append({"url": url, "title": htmlmod.unescape(title.group(1).strip()) if title else anchor, "kind": "strona",
                         "pages": [(1, text[:60000])], "score": score, "sha256": hashlib.sha256(body).hexdigest(), "mime": "text/html", "bytes": len(body)})
        lp = _Links()
        try:
            lp.feed(raw)
        except Exception:
            continue
        for href, a in lp.links:
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absu = urllib.parse.urljoin(url, href).split("#")[0]
            absu = re.sub(r"[?&](fontsize|print|lang|contrast|view)=[^&]*", "", absu)
            if urllib.parse.urlsplit(absu).netloc != domain or absu in seen:
                continue
            fa = fold(a + " " + href)
            p = 0.0
            p += 3.0 * sum(1 for k in kws if k in fa)
            if re.search(NAV_WORDS, fa):
                p += 1.0
            if absu.lower().split("?")[0].endswith(DOC_EXT):
                p += 0.5 + (2.0 if any(k in fa for k in kws) else -0.6)
            if p > 0:
                queue.append((p, absu, a[:200]))
        if progress:
            progress(f"BIP: {pages}/{max_pages} stron, trafień: {len(hits)}")
        time.sleep(delay)
    hits.sort(key=lambda h: -h["score"])
    return hits[:15]


def nik_search(keywords: str, max_results: int = 8) -> list[dict]:
    """Wyszukiwarka NIK (wyniki kontroli) – zwraca linki do informacji o wynikach kontroli."""
    url = "https://www.nik.gov.pl/szukaj/?" + urllib.parse.urlencode({"q": keywords})
    try:
        st, body, _ = http(url, timeout=25, retries=0)
    except Exception:
        return []
    if st != 200:
        return []
    lp = _Links()
    lp.feed(body.decode("utf-8", "replace"))
    out = []
    for href, a in lp.links:
        if "/kontrole/" in href and len(a) > 20:
            out.append({"url": urllib.parse.urljoin("https://www.nik.gov.pl", href), "title": a})
    uniq = {o["url"]: o for o in out}
    return list(uniq.values())[:max_results]
