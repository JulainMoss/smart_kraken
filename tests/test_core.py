"""Testy podstawowe (biblioteka standardowa):  python -m unittest discover tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from riskminer.bzp import basis_label, parse_fields  # noqa: E402
from riskminer.classify import Classifier, extract_connections, extract_length_km, time_extension_days  # noqa: E402
from riskminer.importer import KPO_RE, KPO_REGION, parse_annotation  # noqa: E402
from riskminer.query import province_from_text  # noqa: E402
from riskminer.textutil import nip_valid, norm_bzp_number, parse_amount, parse_period_days, wilson  # noqa: E402

PERF = """Ogłoszenie o wykonaniu umowy
SEKCJA IV – PODSTAWOWE INFORMACJE O ZAWARTEJ UMOWIE
 4.1.) Data zawarcia umowy: 2022-09-01
 4.2.) Okres realizacji zamówienia:
 470 dni 4.3.) Dane wykonawcy, z którym zawarto umowę:
 4.4.) Wartość umowy: 14239710,00 PLN
SEKCJA V PRZEBIEG REALIZACJI UMOWY
 5.1.) Czy umowa została wykonana: Tak
 5.2.) Termin wykonania umowy: 2024-03-19
 5.3.) Czy umowę wykonano w pierwotnie określonym terminie: Nie
 5.4.1.) Liczba zmian: 1
 5.4.2.) Numer zmiany: 1
 5.4.3.) Podstawa prawna zmiany:
 art. 455 ust. 2 ustawy
 5.4.4.) Przyczyny dokonania zmian:
 roboty dodatkowe, zamienne, kolizje z istniejącą infrastrukturą
"""


class TestText(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(parse_amount("4,2 mln zł"), 4200000.0)
        self.assertEqual(parse_amount("850 000,50"), 850000.5)
        self.assertEqual(parse_amount("669273,44 PLN"), 669273.44)

    def test_period(self):
        self.assertEqual(parse_period_days("470 dni"), 470)
        self.assertEqual(parse_period_days("Od 2021-02-09 do 2021-07-30"), 171)
        self.assertEqual(parse_period_days("5 miesiące"), 152)

    def test_identifiers(self):
        self.assertTrue(nip_valid("9121715754"))
        self.assertFalse(nip_valid("9121715755"))
        self.assertEqual(norm_bzp_number("2022/BZP 58537"), "2022/BZP 00058537/01")
        m = KPO_RE.search("KPOD.03.15-IW.27-0013/25")
        self.assertEqual(KPO_REGION[int(m.group(1))], "PL30")  # wielkopolskie

    def test_wilson(self):
        lo, hi = wilson(11, 25)
        self.assertTrue(0.25 < lo < 0.35 and 0.55 < hi < 0.65)


class TestParsing(unittest.TestCase):
    def test_fields(self):
        f = {x.label: x.value for x in parse_fields(PERF)}
        self.assertEqual(f["Czy umowę wykonano w pierwotnie określonym terminie"], "Nie")
        self.assertIn("kolizje", f["Przyczyny dokonania zmian"])
        self.assertEqual(basis_label("art. 455 ust. 1 pkt 4 ustawy"), "okoliczności niemożliwe do przewidzenia")

    def test_annotation(self):
        a = parse_annotation("KPO, inwestycja B3.1.1. Wartość przedsięwzięcia: 5 350 782,65 zł; dofinansowanie: 4 333 677,17 zł. Opis: „Budowa sieci.”")
        self.assertAlmostEqual(a["project_value"], 5350782.65)
        self.assertEqual(a["programme"], "KPO")


class TestClassify(unittest.TestCase):
    clf = Classifier()

    def test_subsector(self):
        self.assertEqual(self.clf.classify_subsector("Modernizacja Stacji Uzdatniania Wody w Czernikowie")[0], "suw")
        self.assertEqual(self.clf.classify_subsector("Budowa kanalizacji deszczowej w ciągu drogi wojewódzkiej nr 444")[0], "deszcz")

    def test_risk(self):
        codes = [c["code"] for c in self.clf.classify_risk("W trakcie robót ziemnych odkopano niezinwentaryzowany zbiornik, który kolidował z trasą")]
        self.assertIn("R02", codes)
        self.assertTrue(self.clf.is_administrative("Zmiana nr rachunku bankowego Wykonawcy", []))

    def test_quantities(self):
        self.assertAlmostEqual(extract_length_km("o długości ok. 7,8 km oraz tłocznych o łącznej długości ok. 2,9 km"), 10.7)
        self.assertEqual(extract_connections("wraz z 85 przyłączami do granic działek"), 85)
        self.assertEqual(time_extension_days("wydłużenie terminu realizacji o 2 miesiące"), 61)

    def test_province(self):
        self.assertEqual(province_from_text("Gmina w województwie zachodniopomorskim")[0], "PL32")
        self.assertEqual(province_from_text("woj. pomorskie")[0], "PL22")


if __name__ == "__main__":
    unittest.main()
