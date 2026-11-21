"""Odczyt dokumentów: DOCX, PDF (z warstwą tekstową; strony zachowane do wskazania dowodu), XLSX/XLS, CSV, TXT, HTML,
DOC/ODT/RTF (przez LibreOffice, jeśli zainstalowany). Opcjonalnie OCR skanów (tesseract + pdftoppm, jeśli dostępne).

Każdy dokument zwraca listę stron [(numer_strony, tekst)], żeby każdy wniosek wskazywał stronę lub fragment."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .textutil import html_to_text

NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_docx(data: bytes) -> list[tuple[int, str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(NS_W + "body")
    lines: list[str] = []
    for el in body if body is not None else []:
        if el.tag == NS_W + "p":
            t = "".join(x.text or "" for x in el.iter(NS_W + "t"))
            if t.strip():
                lines.append(t)
        elif el.tag == NS_W + "tbl":
            for tr in el.iter(NS_W + "tr"):
                cells = ["".join(x.text or "" for x in tc.iter(NS_W + "t")).strip() for tc in tr.findall(NS_W + "tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
    # DOCX nie ma stron – dzielimy na „strony” po ~3000 znaków (akapity), żeby lokalizacja była użyteczna
    pages, cur, n = [], [], 0
    for ln in lines:
        cur.append(ln)
        n += len(ln)
        if n > 3000:
            pages.append("\n".join(cur))
            cur, n = [], 0
    if cur:
        pages.append("\n".join(cur))
    return [(i + 1, p) for i, p in enumerate(pages)] or [(1, "")]


def read_pdf(data: bytes, ocr: bool = True) -> list[tuple[int, str]]:
    pages: list[tuple[int, str]] = []
    try:
        from pypdf import PdfReader  # type: ignore
        r = PdfReader(io.BytesIO(data))
        pages = [(i + 1, (p.extract_text() or "")) for i, p in enumerate(r.pages)]
    except ImportError:
        if shutil.which("pdftotext"):
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / "in.pdf"
                src.write_bytes(data)
                out = subprocess.run(["pdftotext", "-layout", str(src), "-"], capture_output=True, timeout=120)
                txt = out.stdout.decode("utf-8", "replace")
                pages = [(i + 1, p) for i, p in enumerate(txt.split("\f")) if p.strip() or i == 0]
    except Exception:
        pages = []
    chars = sum(len(t.strip()) for _, t in pages)
    if ocr and chars < 50 * max(1, len(pages)) and shutil.which("tesseract") and shutil.which("pdftoppm"):
        pages = ocr_pdf(data) or pages
    return pages


def ocr_pdf(data: bytes, lang: str = "pol") -> list[tuple[int, str]]:
    """OCR skanów (wymaga: tesseract z językiem polskim i pdftoppm). Zwraca [] gdy niedostępne."""
    out = []
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        src.write_bytes(data)
        subprocess.run(["pdftoppm", "-r", "200", "-png", str(src), str(Path(td) / "p")], capture_output=True, timeout=600)
        for i, img in enumerate(sorted(Path(td).glob("p*.png"))):
            r = subprocess.run(["tesseract", str(img), "-", "-l", lang], capture_output=True, timeout=300)
            out.append((i + 1, r.stdout.decode("utf-8", "replace")))
    return out


def read_xlsx_rows(data: bytes) -> list[list[str]]:
    """Pierwszy arkusz .xlsx – czysta biblioteka standardowa (openpyxl, jeśli jest, dla pewności formatów)."""
    try:
        import openpyxl  # type: ignore
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        out = []
        for row in ws.iter_rows(values_only=True):
            out.append(["" if v is None else str(v) for v in row])
        return out
    except ImportError:
        pass
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(NS_S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(NS_S + "t")))
        sheet_name = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
        sheet = ET.fromstring(z.read(sheet_name))
        rows = []
        for row in sheet.iter(NS_S + "row"):
            cells = {}
            for c in row.findall(NS_S + "c"):
                ref = c.attrib.get("r", "A1")
                col = 0
                for ch in re.sub(r"\d", "", ref):
                    col = col * 26 + ord(ch) - 64
                v = c.find(NS_S + "v")
                if c.attrib.get("t") == "s" and v is not None:
                    cells[col - 1] = shared[int(v.text)]
                elif c.attrib.get("t") == "inlineStr":
                    cells[col - 1] = "".join(t.text or "" for t in c.iter(NS_S + "t"))
                elif v is not None:
                    cells[col - 1] = v.text or ""
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        return rows


def read_xls_rows(data: bytes) -> list[list[str]]:
    try:
        import xlrd  # type: ignore
        book = xlrd.open_workbook(file_contents=data)
        sh = book.sheet_by_index(0)
        return [[str(sh.cell_value(r, c)) for c in range(sh.ncols)] for r in range(sh.nrows)]
    except ImportError:
        conv = convert_with_libreoffice(data, ".xls", "xlsx")
        return read_xlsx_rows(conv) if conv else []


def read_csv_rows(data: bytes) -> list[list[str]]:
    txt = decode_text(data)
    try:
        dialect = csv.Sniffer().sniff(txt[:5000], delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
    return [r for r in csv.reader(io.StringIO(txt), dialect)]


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1250", "iso-8859-2"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def convert_with_libreoffice(data: bytes, suffix: str, target: str) -> bytes | None:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"in{suffix}"
        src.write_bytes(data)
        subprocess.run([exe, "--headless", "--convert-to", target, "--outdir", td, str(src)], capture_output=True, timeout=180)
        out = list(Path(td).glob(f"in.{target.split(':')[0]}"))
        return out[0].read_bytes() if out else None


def rows_to_text(rows: list[list[str]]) -> str:
    return "\n".join(" | ".join(c for c in r if c) for r in rows if any(r))


def read_any(filename: str, data: bytes) -> dict:
    """Zwraca {'kind': 'table'|'text', 'pages': [(n, tekst)], 'rows': [...]} dla dowolnego obsługiwanego pliku."""
    ext = Path(filename).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        rows = read_xlsx_rows(data)
        return {"kind": "table", "rows": rows, "pages": [(1, rows_to_text(rows))]}
    if ext == ".xls":
        rows = read_xls_rows(data)
        return {"kind": "table", "rows": rows, "pages": [(1, rows_to_text(rows))]}
    if ext in (".csv", ".tsv"):
        rows = read_csv_rows(data)
        return {"kind": "table", "rows": rows, "pages": [(1, rows_to_text(rows))]}
    if ext == ".docx":
        return {"kind": "text", "pages": read_docx(data)}
    if ext == ".pdf":
        return {"kind": "text", "pages": read_pdf(data)}
    if ext in (".html", ".htm"):
        return {"kind": "text", "pages": [(1, html_to_text(decode_text(data)))]}
    if ext in (".doc", ".odt", ".rtf"):
        conv = convert_with_libreoffice(data, ext, "docx")
        if conv:
            return {"kind": "text", "pages": read_docx(conv)}
        if ext == ".rtf":
            return {"kind": "text", "pages": [(1, re.sub(r"\\[a-z]+\d* ?|[{}]", "", decode_text(data)))]}
        raise ValueError("Do plików .doc/.odt potrzebny jest LibreOffice (soffice).")
    if ext == ".json":
        return {"kind": "text", "pages": [(1, decode_text(data))]}
    return {"kind": "text", "pages": [(1, decode_text(data))]}


def full_text(pages: list[tuple[int, str]]) -> str:
    return "\n\n".join(t for _, t in pages)


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.;!?])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ0-9„\"(•-])", text)
    return [p.strip() for p in parts if len(p.strip()) > 25]
