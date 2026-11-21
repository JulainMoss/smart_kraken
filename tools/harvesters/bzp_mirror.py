# bzp_mirror.py  (wersja 1.5)
"""
Serwer lustrzany API BZP dla HackNation (wyzwanie Risk Miner).

Udostępnia na Twoim komputerze kopię ogłoszeń BZP pod tą samą ścieżką i z tymi
samymi parametrami co produkcyjne API e-Zamówień:

    GET /mo-board/api/v1/notice

Klient napisany dla serwera lustrzanego działa na produkcji po zmianie samego
adresu bazowego. Serwer nie ma limitu zapytań i nie zależy od dostępności platformy
e-Zamówienia. Wymaga wyłącznie biblioteki standardowej Pythona 3.10+.

Uruchomienie (w katalogu pakietu):
    python bzp_mirror.py
    python bzp_mirror.py bzp_kopia/notices.jsonl.xz
    python bzp_mirror.py bzp_kopia/notices.jsonl --port 8050
    python bzp_mirror.py --api-2027
Bez argumentu serwer szuka kopii w katalogu bzp_kopia/ obok skryptu. Archiwum (.xz
albo .gz) jest rozpakowywane przy pierwszym uruchomieniu: jednorazowo, potrzebne
ok. 2,6 GB na dysku. Przy pierwszym starcie powstaje też plik indeksu, dzięki któremu
kolejne uruchomienia trwają kilka sekund.
Adres serwera jest wypisywany po starcie; domyślnie http://127.0.0.1:8010.

Port: domyślnie 8010. Gdy jest zajęty, serwer bierze kolejny wolny. Porty używane
domyślnie przez bazy danych (np. 3306, 5432, 1433, 27017) są zawsze pomijane.

Obsługiwane parametry (jak w produkcyjnym API):
    NoticeType            wymagany; dopasowanie dokładne
    PublicationDateFrom   YYYY-MM-DDTHH:MM:SS
    PublicationDateTo     YYYY-MM-DDTHH:MM:SS
    PageSize              domyślnie 10, maksymalnie 500 (w trybie --api-2027: 100)
    SearchAfter           objectId ostatniego ogłoszenia z poprzedniej strony
    OrderType             Delivery / Services / Works
    CpvCode               fragment pola cpvCode: pełny kod (45231300-8), kod bez cyfry
                          kontrolnej (45231300) albo prefiks (452313); kod może być
                          kodem głównym lub dodatkowym
    OrganizationProvince  kod województwa, np. PL02
    OrganizationName      fragment nazwy, bez rozróżniania wielkości liter
    OrganizationCity      nazwa miejscowości
    OrderObject           fragment przedmiotu zamówienia, bez rozróżniania wielkości liter
    NoticeNumber          numer ogłoszenia, z sufiksem wersji lub bez
Nieznane parametry są ignorowane, tak jak w produkcyjnym API. Produkcyjne API nie
filtruje po NIP zamawiającego ani po identyfikatorze postępowania, więc serwer
lustrzany też tego nie robi – po tych polach filtruje klient.

Kopia nie zawiera wszystkich typów ogłoszeń. Zapytanie o typ, którego w kopii nie
ma, zwraca pustą listę i nagłówek X-Mirror-Missing-Type. Zawartość kopii pokazuje
adres /info oraz komunikat po starcie.

Różnice względem produkcji, które mogą mieć znaczenie: kolejność wyników (domyślnie
od najnowszych; --order asc ją odwraca) i domyślny PageSize nie są udokumentowane
przez UZP, więc nie należy na nich polegać.

Tryb --api-2027 włącza zachowanie zapowiedziane dla produkcyjnego API od 11.01.2027
(https://ezamowienia.gov.pl/pl/planowane-zmiany-w-api-bzp/): PageSize najwyżej 100
i odrzucanie nieznanych parametrów z kodem 404. PageSize powyżej 100 jest w tym
trybie odrzucany z kodem 400.

Zmiany w 1.5:
- obsługa archiwum .xz (kilka razy mniejsze od .gz przy tej samej zawartości),
- indeks ogłoszeń zapisywany obok kopii, więc tylko pierwszy start czyta cały plik,
- po podmianie archiwum na nowsze kopia jest rozpakowywana od nowa.

Zmiany w 1.4:
- plik kopii nie musi być podany: serwer szuka go w bzp_kopia/ obok skryptu,
- kopia spakowana jest rozpakowywana przy pierwszym uruchomieniu,
- porty domyślne baz danych są pomijane przy wyborze portu,
- zapytanie o typ ogłoszeń spoza kopii dostaje nagłówek X-Mirror-Missing-Type,
- komunikat po starcie pokazuje, jakie typy ogłoszeń są w kopii.

Wcześniejsze wersje: 1.3 – treść ogłoszeń czytana z pliku (mało pamięci), wybór
wolnego portu; 1.2 – --order i --api-2027; 1.1 – CpvCode jako fragment, OrderObject.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

API_PATH = "/mo-board/api/v1/notice"
DEFAULT_PORT = 8010
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 500
PORT_ATTEMPTS = 20  # ile kolejnych portów sprawdzić, gdy podany jest zajęty
MAX_PAGE_SIZE_2027 = 100  # zapowiedziane maksimum od 11.01.2027

# Miejsca, w których serwer szuka kopii, gdy plik nie został podany; archiwum ma
# pierwszeństwo, bo po podmianie paczki rozpakowany plik może być nieaktualny
PACKED_SUFFIXES = (".xz", ".gz")
DEFAULT_SNAPSHOTS = (
    "bzp_kopia/notices.jsonl.xz",
    "bzp_kopia/notices.jsonl.gz",
    "bzp_kopia/notices.jsonl",
)
INDEX_VERSION = 1

# Domyślne porty baz danych – serwer nigdy ich nie zajmuje
RESERVED_PORTS = {
    1433,  # Microsoft SQL Server
    1434,
    1521,  # Oracle
    3050,  # Firebird
    3306,  # MySQL, MariaDB
    33060,
    5432,  # PostgreSQL
    5433,
    6379,  # Redis
    8123,  # ClickHouse
    9000,
    9042,  # Cassandra
    9200,  # Elasticsearch, OpenSearch
    26257,  # CockroachDB
    27017,  # MongoDB
    50000,  # Db2
}

# Parametry obsługiwane przez serwer; w trybie --api-2027 każdy inny daje 404
KNOWN_PARAMS = {
    "NoticeType",
    "PublicationDateFrom",
    "PublicationDateTo",
    "PageSize",
    "SearchAfter",
    "OrderType",
    "CpvCode",
    "OrganizationProvince",
    "OrganizationName",
    "OrganizationCity",
    "OrderObject",
    "NoticeNumber",
}

NOTICE_TYPES = {
    "ContractNotice",
    "TenderResultNotice",
    "NoticeUpdateNotice",
    "SmallContractNotice",
    "ContractPerformingNotice",
    "AgreementUpdateNotice",
    "AgreementIntentionNotice",
    "CompetitionNotice",
    "CompetitionResultNotice",
}

# Kolejność, w jakiej typy są pokazywane w komunikatach
MAIN_TYPES = (
    "ContractNotice",
    "TenderResultNotice",
    "AgreementUpdateNotice",
    "ContractPerformingNotice",
)

BLOCKED_HTML = (
    '<!DOCTYPE html>\n<html lang="pl">\n<head>\n<meta charset="UTF-8">\n'
    "<title>Dostęp zablokowany / Access Blocked</title>\n</head>\n"
    "<body><h1>Dostęp zablokowany / Access Blocked</h1>"
    "<p>Przekroczono limit zapytań (serwer lustrzany HackNation).</p></body>\n</html>\n"
)


# ---------------------------------------------------------------------------
# Plik kopii
# ---------------------------------------------------------------------------


def expected_size(packed: Path) -> int | None:
    """Rozmiar po rozpakowaniu w bajtach wg manifest.json obok archiwum, jeśli jest."""
    manifest = packed.with_name("manifest.json")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return int(float(data["size_unpacked_mb"]) * 2**20)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def open_packed(packed: Path):
    if packed.suffix == ".gz":
        return gzip.open(packed, "rb")
    try:
        import lzma
    except ImportError:
        raise SystemExit(
            "Ta instalacja Pythona nie obsługuje plików .xz. Rozpakuj plik programem 7-Zip "
            "albo poleceniem xz -dk i podaj plik .jsonl."
        ) from None
    return lzma.open(packed, "rb")


def unpack(packed: Path) -> Path:
    """Rozpakowuje archiwum obok niego; raz rozpakowany plik jest używany ponownie."""
    target = packed.with_suffix("")
    # znacznik pamięta rozmiar archiwum, z którego powstał plik – nowa paczka go unieważnia
    marker = target.with_name(target.name + ".ok")
    stamp = str(packed.stat().st_size)
    if target.exists() and marker.exists() and marker.read_text().strip() == stamp:
        return target
    size = expected_size(packed)
    free = shutil.disk_usage(packed.parent).free
    if size and free < size + 100 * 2**20:
        raise SystemExit(
            f"Za mało miejsca na dysku: potrzeba ok. {size / 2**30:.1f} GB, wolne {free / 2**30:.1f} GB."
        )
    about = f"ok. {size / 2**30:.1f} GB" if size else "kilka GB"
    print(f"Rozpakowuję {packed.name} (jednorazowo, {about})...", flush=True)
    partial = target.with_name(target.name + ".part")
    written = 0
    with open_packed(packed) as src, partial.open("wb") as dst:
        while chunk := src.read(8 * 2**20):
            dst.write(chunk)
            before, written = written, written + len(chunk)
            if written // 2**29 > before // 2**29:
                print(f"  rozpakowano {written / 2**30:.1f} GB", flush=True)
    os.replace(partial, target)
    marker.write_text(stamp)
    return target


def resolve_snapshot(given: Path | None) -> Path:
    """Zwraca rozpakowany plik kopii; szuka go obok skryptu, gdy nie został podany."""
    if given is None:
        roots = (Path(__file__).resolve().parent, Path.cwd())
        candidates = [root / name for root in roots for name in DEFAULT_SNAPSHOTS]
        given = next((p for p in candidates if p.exists()), None)
        if given is None:
            raise SystemExit(
                "Nie znaleziono kopii ogłoszeń. Podaj plik: python bzp_mirror.py bzp_kopia/notices.jsonl.xz"
            )
    if not given.exists():
        # podano plik .jsonl, a w katalogu jest jeszcze tylko archiwum
        packed = next(
            (
                p
                for p in (given.with_name(given.name + ext) for ext in PACKED_SUFFIXES)
                if p.exists()
            ),
            None,
        )
        if packed is None:
            raise SystemExit(f"Nie ma pliku kopii: {given}")
        given = packed
    if given.suffix in PACKED_SUFFIXES:
        return unpack(given)
    if given.suffix in (".zip", ".zst", ".7z"):
        raise SystemExit(
            "Plik kopii jest spakowany – rozpakuj go i podaj plik .jsonl, .jsonl.xz albo .jsonl.gz"
        )
    return given


# Pola potrzebne do filtrowania i sortowania – tylko one zostają w pamięci
INDEX_FIELDS = (
    "objectId",
    "noticeType",
    "noticeNumber",
    "publicationDate",
    "orderType",
    "cpvCode",
    "orderObject",
    "organizationName",
    "organizationCity",
    "organizationProvince",
)


def load_notices(path: Path) -> list[dict]:
    """Indeks ogłoszeń; przy kolejnych startach czytany z pliku obok kopii."""
    cache = path.with_name(path.name + ".idx")
    size = path.stat().st_size
    keys = (*INDEX_FIELDS, "_offset", "_length")
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if saved.get("version") == INDEX_VERSION and saved.get("size") == size:
            return [dict(zip(keys, row)) for row in saved["rows"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    print("  buduję indeks (tylko przy pierwszym uruchomieniu)...", flush=True)
    notices = build_index(path)
    try:
        cache.write_text(
            json.dumps(
                {
                    "version": INDEX_VERSION,
                    "size": size,
                    "rows": [[entry[k] for k in keys] for entry in notices],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # katalog tylko do odczytu – indeks zostanie zbudowany ponownie
    return notices


def build_index(path: Path) -> list[dict]:
    """Indeks ogłoszeń: pola do filtrowania oraz położenie rekordu w pliku."""
    notices: dict[str, dict] = {}
    offset = 0
    with path.open("rb") as f:
        for raw in f:
            length = len(raw)
            line = raw.strip()
            if line:
                item = json.loads(line)
                if item.get("objectId"):
                    entry = {name: item.get(name) for name in INDEX_FIELDS}
                    entry["_offset"] = offset
                    entry["_length"] = length
                    notices[item["objectId"]] = entry
            offset += length
    # kolejność rosnąca po dacie publikacji; main() odwraca ją dla --order desc
    return sorted(
        notices.values(),
        key=lambda d: (d.get("publicationDate") or "", d.get("objectId") or ""),
    )


def read_records(path: Path, entries: list[dict]) -> bytes:
    """Treść odpowiedzi: rekordy przepisane z pliku bez ponownego kodowania."""
    parts = []
    with path.open("rb") as f:
        for entry in entries:
            f.seek(entry["_offset"])
            parts.append(f.read(entry["_length"]).strip())
    return b"[" + b",".join(parts) + b"]"


def bzp_base(number: str) -> str:
    # "2026/BZP 00159936/01" -> "2026/BZP 00159936"
    m = re.search(r"(\d{4})\s*/\s*BZP\s*0*(\d+)", number or "", re.IGNORECASE)
    return f"{m.group(1)}/BZP {int(m.group(2)):08d}" if m else (number or "").strip()


def matches(item: dict, q: dict[str, str]) -> bool:
    if item.get("noticeType") != q["NoticeType"]:
        return False
    published = (item.get("publicationDate") or "")[:19]
    if "PublicationDateFrom" in q and published < q["PublicationDateFrom"][:19]:
        return False
    if "PublicationDateTo" in q and published > q["PublicationDateTo"][:19]:
        return False
    if "OrderType" in q and (item.get("orderType") or "") != q["OrderType"]:
        return False
    if "CpvCode" in q:
        # jak na produkcji: fragment pola cpvCode (pełny kod, kod bez cyfry kontrolnej, prefiks)
        if (
            q["CpvCode"].strip().casefold()
            not in (item.get("cpvCode") or "").casefold()
        ):
            return False
    if (
        "OrganizationProvince" in q
        and (item.get("organizationProvince") or "") != q["OrganizationProvince"]
    ):
        return False
    if "OrganizationName" in q:
        if (
            q["OrganizationName"].casefold()
            not in (item.get("organizationName") or "").casefold()
        ):
            return False
    if "OrderObject" in q:
        if (
            q["OrderObject"].strip().casefold()
            not in (item.get("orderObject") or "").casefold()
        ):
            return False
    if "OrganizationCity" in q:
        if (
            q["OrganizationCity"].casefold()
            != (item.get("organizationCity") or "").casefold()
        ):
            return False
    if "NoticeNumber" in q:
        wanted = q["NoticeNumber"].strip()
        number = item.get("noticeNumber") or ""
        if wanted != number and bzp_base(wanted) != bzp_base(number):
            return False
        # numer z sufiksem wersji musi zgadzać się w całości
        if re.search(r"/\d{2}$", wanted) and wanted != number:
            return False
    return True


# ---------------------------------------------------------------------------
# Limit zapytań (opcjonalny, do testów odporności klienta)
# ---------------------------------------------------------------------------


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        with self.lock:
            window = self.hits[ip]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self.per_minute:
                return False
            window.append(now)
            return True


# ---------------------------------------------------------------------------
# Serwer
# ---------------------------------------------------------------------------


class MirrorServer(ThreadingHTTPServer):
    daemon_threads = True
    # w Windows ponowne użycie adresu pozwala zająć port używany przez inny program
    allow_reuse_address = os.name != "nt"


def make_handler(
    snapshot: Path,
    notices: list[dict],
    limiter: RateLimiter,
    info: dict,
    verbose: bool,
    api_2027: bool = False,
):
    max_page_size = MAX_PAGE_SIZE_2027 if api_2027 else MAX_PAGE_SIZE
    present_types = set(info["by_type"])
    warned: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        server_version = "BzpMirror/1.5"

        def log_message(self, fmt, *args):
            if verbose:
                super().log_message(fmt, *args)

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Data-Source", "kopia BZP - serwer lustrzany HackNation")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/info"):
                self._json(200, info)
                return
            if url.path.rstrip("/") != API_PATH:
                self._json(404, {"title": "Not Found", "status": 404})
                return
            if not limiter.allow(self.client_address[0]):
                self._send(
                    403, BLOCKED_HTML.encode("utf-8"), "text/html; charset=utf-8"
                )
                return

            q = {k: v[0] for k, v in parse_qs(url.query).items() if v}
            unknown = sorted(set(q) - KNOWN_PARAMS)
            if api_2027 and unknown:
                # zapowiedź UZP: niepoprawne parametry (np. pageNumber) -> 404
                self._json(
                    404,
                    {
                        "title": "Not Found",
                        "status": 404,
                        "detail": "Niepoprawne parametry zapytania: "
                        + ", ".join(unknown),
                    },
                )
                return
            notice_type = q.get("NoticeType", "")
            if notice_type not in NOTICE_TYPES:
                # treść błędu jak w produkcyjnym API
                self._json(
                    400,
                    {
                        "errors": {
                            "NoticeType": [
                                f"Pole 'Notice Type' ma zakres wartości, który nie obejmuje {notice_type}."
                            ]
                        },
                        "type": "https://tools.ietf.org/html/rfc7231#section-6.5.1",
                        "title": "One or more validation errors occurred.",
                        "status": 400,
                    },
                )
                return

            try:
                page_size = int(q.get("PageSize", DEFAULT_PAGE_SIZE))
            except ValueError:
                page_size = DEFAULT_PAGE_SIZE
            if api_2027 and page_size > max_page_size:
                self._json(
                    400,
                    {
                        "errors": {
                            "PageSize": [
                                f"Pole 'Page Size' musi być w zakresie od 1 do {max_page_size}."
                            ]
                        },
                        "title": "One or more validation errors occurred.",
                        "status": 400,
                    },
                )
                return
            page_size = max(1, min(page_size, max_page_size))

            # typ poprawny, ale nieobecny w kopii: pusta lista z wyraźnym sygnałem
            headers = {}
            if notice_type not in present_types:
                headers["X-Mirror-Missing-Type"] = notice_type
                if notice_type not in warned:
                    warned.add(notice_type)
                    print(
                        f"Uwaga: kopia nie zawiera ogłoszeń typu {notice_type} – odpowiedź jest pusta.",
                        flush=True,
                    )

            selected = [d for d in notices if matches(d, q)]
            search_after = q.get("SearchAfter")
            if search_after:
                index = next(
                    (
                        i
                        for i, d in enumerate(selected)
                        if d.get("objectId") == search_after
                    ),
                    None,
                )
                selected = selected[index + 1 :] if index is not None else []
            self._send(
                200,
                read_records(snapshot, selected[:page_size]),
                "application/json; charset=utf-8",
                headers,
            )

    return Handler


def bind(host: str, first_port: int, handler) -> tuple[MirrorServer, int]:
    """Zajmuje pierwszy wolny port, pomijając domyślne porty baz danych."""
    tried = 0
    port = first_port
    while tried < PORT_ATTEMPTS and port < 65536:
        if port in RESERVED_PORTS:
            print(f"Port {port} jest domyślnym portem bazy danych – pomijam.")
            port += 1
            continue
        tried += 1
        try:
            return MirrorServer((host, port), handler), port
        except OSError:
            print(f"Port {port} jest zajęty – próbuję następny.")
            port += 1
    raise SystemExit(
        f"Brak wolnego portu od {first_port} (sprawdzono {tried}); podaj inny: --port NUMER"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serwer lustrzany API BZP (HackNation, Risk Miner)"
    )
    parser.add_argument(
        "snapshot",
        type=Path,
        nargs="?",
        help="plik kopii (.jsonl albo .jsonl.gz); domyślnie bzp_kopia/notices.jsonl obok skryptu",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="adres nasłuchu; 0.0.0.0 udostępnia w sieci"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=0,
        help="limit zapytań na minutę z jednego IP (0 = bez limitu)",
    )
    parser.add_argument(
        "--order",
        choices=("desc", "asc"),
        default="desc",
        help="kolejność wyników wg daty publikacji (domyślnie od najnowszych)",
    )
    parser.add_argument(
        "--api-2027",
        action="store_true",
        help="zachowanie API zapowiedziane od 11.01.2027: PageSize do 100, nieznane parametry -> 404",
    )
    parser.add_argument("--verbose", action="store_true", help="loguj każde zapytanie")
    args = parser.parse_args()

    snapshot = resolve_snapshot(args.snapshot)
    print(f"Wczytuję indeks ogłoszeń z {snapshot}...", flush=True)
    notices = load_notices(snapshot)
    by_type: dict[str, int] = defaultdict(int)
    for d in notices:
        by_type[d.get("noticeType") or "?"] += 1
    missing = [t for t in MAIN_TYPES if t not in by_type]
    info = {
        "service": "Serwer lustrzany API BZP – HackNation, wyzwanie Risk Miner",
        "endpoint": API_PATH,
        "notices": len(notices),
        "by_type": dict(by_type),
        "missing_types": sorted(NOTICE_TYPES - set(by_type)),
        "publication_date_from": (notices[0].get("publicationDate") or "")[:10]
        if notices
        else None,
        "publication_date_to": (notices[-1].get("publicationDate") or "")[:10]
        if notices
        else None,
        "rate_limit_per_minute": args.rate_limit or None,
        "order": args.order,
        "max_page_size": MAX_PAGE_SIZE_2027 if args.api_2027 else MAX_PAGE_SIZE,
        "unknown_parameters": "404" if args.api_2027 else "ignorowane",
        "note": "Kopia danych publicznych z ezamowienia.gov.pl; produkcyjne API: https://ezamowienia.gov.pl"
        + API_PATH,
    }

    if args.order == "desc":
        notices = notices[::-1]
    handler = make_handler(
        snapshot,
        notices,
        RateLimiter(args.rate_limit),
        info,
        args.verbose,
        args.api_2027,
    )
    server, port = bind(args.host, args.port, handler)

    base = f"http://{args.host}:{port}"
    example_type = next((t for t in MAIN_TYPES[::-1] if t in by_type), "ContractNotice")
    print(f"Wczytano ogłoszeń: {len(notices)}")
    for name in sorted(by_type, key=lambda t: -by_type[t]):
        print(f"  {name}: {by_type[name]}")
    if notices:
        print(
            f"  daty publikacji: {info['publication_date_from']} – {info['publication_date_to']}"
        )
    if missing:
        print(f"Kopia nie zawiera typów: {', '.join(missing)}")
    print(f"\nSerwer lustrzany działa: {base}{API_PATH}")
    print(f"Przykład: {base}{API_PATH}?NoticeType={example_type}&PageSize=10")
    print(f"Opis kopii: {base}/info")
    print(
        f"Kolejność: {args.order}; tryb API 2027: {'tak' if args.api_2027 else 'nie'}; zatrzymanie: Ctrl+C"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymano.")


if __name__ == "__main__":
    main()
