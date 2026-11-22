"""Prezentacja (docs/prezentacja.html -> docs/prezentacja.pdf): autorzy na pierwszym slajdzie, PDF aktualny względem HTML.

    python -m unittest tests.test_prezentacja
PDF odtwarza:  python tools/prezentacja_pdf.py
"""

import html
import re
import shutil
import subprocess
import unittest
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
HTML = DOCS / "prezentacja.html"
PDF = DOCS / "prezentacja.pdf"
AUTHORS = ["Julian Mossakowski", "Stanisław Reizer"]


def _plain(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def slides() -> list[str]:
    """Treść kolejnych slajdów (<section>) jako zwykły tekst."""
    return [_plain(s) for s in re.findall(r"<section\b.*?</section>", HTML.read_text(encoding="utf-8"), re.S)]


def headings() -> list[str]:
    """Tytuł (h1/h2) każdego slajdu."""
    out = []
    for s in re.findall(r"<section\b.*?</section>", HTML.read_text(encoding="utf-8"), re.S):
        m = re.search(r"<h[12][^>]*>(.*?)</h[12]>", s, re.S)
        out.append(_plain(m.group(1)) if m else "")
    return out


def pdf_pages() -> list[str] | None:
    """Tekst stron PDF (pypdf albo pdftotext); None, gdy nie ma czym czytać."""
    try:
        from pypdf import PdfReader
        return [p.extract_text() or "" for p in PdfReader(str(PDF)).pages]
    except ImportError:
        pass
    if shutil.which("pdftotext"):
        out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(PDF), "-"], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
        return out.split("\f")[:-1] if out.endswith("\f") else out.split("\f")
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


class TestPrezentacja(unittest.TestCase):
    def test_authors_on_first_slide_html(self):
        first = slides()[0]
        for name in AUTHORS:
            self.assertIn(name, first, f"brak „{name}” na pierwszym slajdzie docs/prezentacja.html")

    def test_pdf_matches_html(self):
        self.assertTrue(PDF.exists(), "brak docs/prezentacja.pdf – uruchom: python tools/prezentacja_pdf.py")
        pages = pdf_pages()
        if pages is None:
            self.skipTest("brak pypdf i pdftotext – nie można odczytać PDF")
        heads = headings()
        self.assertEqual(len(pages), len(heads), "liczba stron PDF różna od liczby slajdów – odtwórz PDF: python tools/prezentacja_pdf.py")
        for name in AUTHORS:
            self.assertIn(_norm(name), _norm(pages[0]), f"brak „{name}” na pierwszej stronie PDF – odtwórz PDF: python tools/prezentacja_pdf.py")
        for i, (page, head) in enumerate(zip(pages, heads), 1):
            words = [w for w in re.findall(r"\w{4,}", head)][:4]   # pierwsze słowa tytułu (odporne na łamanie wierszy)
            for w in words:
                self.assertIn(_norm(w), _norm(page), f"slajd {i}: „{head}” nie zgadza się z PDF – odtwórz PDF: python tools/prezentacja_pdf.py")


if __name__ == "__main__":
    unittest.main()
