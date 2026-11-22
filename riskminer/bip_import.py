"""Dodawanie danych z BIP przez urzędnika: wklejony adres strony BIP (albo bezpośredni link do PDF/DOCX)
lub skopiowana treść strony. System pobiera stronę i jej załączniki (protokoły, aneksy, umowy, zarządzenia),
zapisuje je jako dokumenty z adresem źródła i sumą SHA-256, rozpoznaje fragmenty wskazujące na ryzyko
(z numerem strony) i – opcjonalnie – przypina je do projektu. Wszystko trafia do weryfikacji eksperta."""

from __future__ import annotations

import hashlib
import html as htmlmod
import re
import sqlite3
import urllib.parse
from typing import Any, Callable

from .docs import full_text, read_any
from .importer import _add_link, _doc_type, _store_doc, analyze_document
from .db import log_source
from .sources import http
from .sources.external import DOC_EXT, _Links
from .textutil import html_to_text

Say = Callable[[str], None]
MAX_ATTACHMENTS = 15


def _is_doc(url: str, ctype: str) -> bool:
    return url.lower().split("?")[0].endswith(DOC_EXT) or any(x in ctype for x in ("pdf", "officedocument", "msword", "opendocument", "excel"))


def _store(conn, project_id, title, url, pages, body: bytes, mime: str, kind: str) -> dict[str, Any]:
    sha = hashlib.sha256(body).hexdigest()
    doc_id = "BIP-" + sha[:16]
    text = full_text(pages)
    _store_doc(conn, doc_id, project_id, None, "BIP", _doc_type(title, text), title[:300], url, pages, sha=sha, mime=mime,
               meta={"dodano": "ręcznie (adres BIP)", "rodzaj": kind})
    if project_id:
        _add_link(conn, project_id, "document", doc_id, title[:300], "BIP", url, "dokument z BIP wskazany przez użytkownika", 1.0,
                  [f"adres: {url}"], "approved")
    n = analyze_document(conn, doc_id)
    conn.commit()
    return {"doc_id": doc_id, "title": title[:300], "url": url, "kind": kind, "pages": len(pages), "chars": len(text), "events": n}


def import_bip_url(conn: sqlite3.Connection, url: str, project_id: str | None = None, attachments: bool = True,
                   say: Say = print) -> list[dict[str, Any]]:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    say(f"Pobieram {url}")
    st, body, hd = http(url, timeout=25, retries=1, max_bytes=25_000_000)
    if st != 200 or not body:
        raise ValueError(f"Strona {url} zwróciła kod {st}.")
    ctype = hd.get("content-type", "")
    out: list[dict[str, Any]] = []
    if _is_doc(url, ctype):
        name = urllib.parse.unquote(url.split("?")[0].rsplit("/", 1)[-1]) or "dokument.pdf"
        if "pdf" in ctype and not name.lower().endswith(".pdf"):
            name += ".pdf"
        parsed = read_any(name, body)
        out.append(_store(conn, project_id, name, url, parsed["pages"], body, ctype, "dokument"))
        say(f"Dokument {name}: {len(parsed['pages'])} str., fragmentów ryzyka: {out[-1]['events']}")
        log_source(conn, "BIP", "dodanie dokumentu z BIP", url, st, 1, sha256=hashlib.sha256(body).hexdigest())
        return out
    try:
        raw = body.decode("utf-8")
    except UnicodeDecodeError:
        raw = body.decode("cp1250", "replace")
    m = re.search(r"<title>(.*?)</title>", raw, re.S | re.I)
    title = re.sub(r"\s+", " ", htmlmod.unescape(m.group(1))).strip() if m else url
    text = html_to_text(raw)
    out.append(_store(conn, project_id, title, url, [(1, text[:200000])], body, "text/html", "strona"))
    say(f"Strona „{title[:80]}”: {len(text):,} znaków, fragmentów ryzyka: {out[-1]['events']}".replace(",", " "))
    if attachments:
        lp = _Links()
        try:
            lp.feed(raw)
        except Exception:
            pass
        docs, seen = [], set()
        for href, anchor in lp.links:
            absu = urllib.parse.urljoin(url, href).split("#")[0]
            if absu in seen or not (absu.lower().split("?")[0].endswith(DOC_EXT) or re.search(r"download|pobierz|attachment|zalacznik|plik", absu.lower())):
                continue
            seen.add(absu)
            docs.append((absu, anchor))
        if docs:
            say(f"Załączniki na stronie: {len(docs)}" + (f" (pobieram pierwsze {MAX_ATTACHMENTS})" if len(docs) > MAX_ATTACHMENTS else ""))
        for absu, anchor in docs[:MAX_ATTACHMENTS]:
            try:
                st2, b2, h2 = http(absu, timeout=25, retries=1, max_bytes=25_000_000)
                c2 = h2.get("content-type", "")
                if st2 != 200 or not b2 or not _is_doc(absu, c2):
                    continue
                name = urllib.parse.unquote(absu.split("?")[0].rsplit("/", 1)[-1]) or "zalacznik"
                if "pdf" in c2 and not name.lower().endswith(".pdf"):
                    name += ".pdf"
                parsed = read_any(name, b2)
                # „Zał. nr 4 - wzór umowy 2.08 MB pdf Pobierz” -> „Zał. nr 4 - wzór umowy”
                label = re.sub(r"\s*\(?\d+(?:[.,]\d+)?\s*(?:[kKMG]?B|kb|mb)\)?.*$|\s*\b(pobierz|download|pdf|docx?|odt)\b\s*$", "", (anchor or "").strip(), flags=re.I)
                label = (label.strip(" -–|") or name)[:200]
                out.append(_store(conn, project_id, label, absu, parsed["pages"], b2, c2, "załącznik"))
                say(f"  załącznik „{label[:70]}”: {len(parsed['pages'])} str., fragmentów ryzyka: {out[-1]['events']}")
            except Exception as e:
                say(f"  pominięto {absu[:90]} ({type(e).__name__})")
    log_source(conn, "BIP", "dodanie strony BIP z załącznikami", url, st, len(out), sha256=hashlib.sha256(body).hexdigest())
    conn.commit()
    return out


def import_bip_text(conn: sqlite3.Connection, text: str, title: str = "", url: str = "", project_id: str | None = None) -> dict[str, Any]:
    """Treść skopiowana ze strony BIP (gdy strona wymaga logowania lub blokuje roboty)."""
    text = text.strip()
    if len(text) < 40:
        raise ValueError("Wklejona treść jest za krótka.")
    title = title.strip() or (text.splitlines()[0][:120] if text else "treść z BIP")
    res = _store(conn, project_id, title, url.strip() or None, [(1, text)], text.encode("utf-8"), "text/plain", "treść wklejona")
    log_source(conn, "BIP", "wklejenie treści z BIP", url or title, 200, 1)
    return res


def import_bip(conn: sqlite3.Connection, urls: list[str], text: str = "", title: str = "", project_id: str | None = None,
               attachments: bool = True, say: Say = print) -> dict[str, Any]:
    docs: list[dict[str, Any]] = []
    errors: list[str] = []
    for u in [u.strip() for u in urls if u and u.strip()]:
        try:
            docs.extend(import_bip_url(conn, u, project_id, attachments, say))
        except Exception as e:
            errors.append(f"{u}: {e}")
            say(f"Nie udało się pobrać {u}: {e}")
    if text and text.strip():
        docs.append(import_bip_text(conn, text, title, urls[0] if len(urls) == 1 else "", project_id))
        say(f"Zapisano wklejoną treść: fragmentów ryzyka: {docs[-1]['events']}")
    if not docs and errors:
        raise ValueError("; ".join(errors))
    total = sum(d["events"] for d in docs)
    say(f"Gotowe: {len(docs)} dokumentów, {total} fragmentów wskazujących na ryzyko (czekają na weryfikację eksperta).")
    return {"documents": docs, "events": total, "errors": errors}
