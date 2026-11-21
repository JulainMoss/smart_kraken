"""Konektory źródeł publicznych. Każdy zapisuje operację w rejestrze `sources` (URL, status, liczba rekordów, czas),
a pobrane treści w `notices` / `documents` z datą pobrania i sumą SHA-256 – każdy rekord prowadzi do źródła."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import config


def http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 25, retries: int = 2,
         max_bytes: int | None = None) -> tuple[int, bytes, dict]:
    hdrs = {"User-Agent": config.get()["sources"]["user_agent"], "Accept": "*/*", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                hd = {k.lower(): v for k, v in r.headers.items()}
                if max_bytes:
                    if int(hd.get("content-length") or 0) > max_bytes:
                        return 413, b"", hd
                    body = r.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        return 413, b"", hd
                    return r.status, body, hd
                return r.status, r.read(), hd
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return e.code, e.read() if hasattr(e, "read") else b"", {}
        except Exception as e:  # sieć – ponawiamy
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
    raise ConnectionError(f"{url}: {last}")


def get_json(url: str, params: dict | None = None, timeout: float = 25) -> Any:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    st, body, _ = http(url, headers={"Accept": "application/json"}, timeout=timeout)
    if st != 200:
        raise ConnectionError(f"HTTP {st} {url}")
    return json.loads(body.decode("utf-8"))
