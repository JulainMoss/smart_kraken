"""Odtwarza docs/prezentacja.pdf z docs/prezentacja.html (przeglądarka Chrome / Edge / Chromium w trybie headless)
i sprawdza wynik (autorzy na pierwszym slajdzie, strony zgodne ze slajdami).

    python tools/prezentacja_pdf.py [--browser ŚCIEŻKA]
"""

import argparse
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "docs" / "prezentacja.html"
PDF = ROOT / "docs" / "prezentacja.pdf"


def find_browser():
    names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"]
    cands = [shutil.which(n) for n in names]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                      os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    cands.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return next((c for c in cands if c and os.path.isfile(c)), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--browser", help="ścieżka do chrome / msedge")
    a = ap.parse_args()
    exe = a.browser or find_browser()
    if not exe:
        sys.exit("Nie znaleziono Chrome / Edge / Chromium – podaj --browser ŚCIEŻKA.")
    cmd = [exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
           "--virtual-time-budget=5000", f"--print-to-pdf={PDF}", HTML.as_uri()]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    print(f"Zapisano {PDF}")
    sys.path.insert(0, str(ROOT))
    from tests import test_prezentacja
    res = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(test_prezentacja))
    sys.exit(0 if res.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
