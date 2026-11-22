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


class TestQueryUnderstanding(unittest.TestCase):
    """Odczyt opisu inwestycji: reguły + (symulowany) model językowy, weryfikacja cytatów, opis zbyt ogólny."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from riskminer import db
        cls.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.tmp.name) / "t.sqlite"
        cls.conn = db.connect()
        db.seed_knowledge(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_casual_duration_and_vague(self):
        from riskminer.classify import extract_duration_days
        from riskminer.query import understand
        self.assertEqual(extract_duration_days("Mamy 6 mln zł i rok na realizację.")["days"], 365)
        self.assertEqual(extract_duration_days("Prace potrwają półtora roku.")["days"], 548)
        q = understand(self.conn, "Proszę o pomoc, planujemy jakąś inwestycję w naszej gminie w przyszłym roku.", use_llm=False)
        self.assertTrue(q["vague"])
        q = understand(self.conn, "Budowa kanalizacji sanitarnej 5 km z przepompownią, budżet 6 mln zł.", use_llm=False)
        self.assertFalse(q["vague"])

    def test_verify_quote(self):
        from riskminer.query import verify_quote
        text = "Gmina Lipowo planuje rozbudowę sieci kanalizacji sanitarnej o długości 7,8 km."
        self.assertTrue(verify_quote("rozbudowę sieci kanalizacji", text))
        self.assertTrue(verify_quote("Rozbudowę  sieci kanalizacji sanitarnej", text))
        self.assertFalse(verify_quote("budowa oczyszczalni ścieków w Olsztynie", text))

    def test_model_fills_form(self):
        from unittest import mock
        from riskminer import llm
        from riskminer.query import understand
        text = ("Chcemy zbudować kanalizację w naszej gminie Lipowo w województwie warmińsko-mazurskim, mniej więcej 5 km sieci "
                "i ze 40 przyłączy. Mamy około 6 mln zł brutto. Wykonawca ma też zaprojektować sieć.")
        answer = {
            "czy_konkretna_inwestycja": True,
            "rodzaj_inwestycji": {"wartosc": "kanalizacja sanitarna", "cytat": "zbudować kanalizację"},
            "rodzaj_robot": {"wartosc": "budowa", "cytat": "Chcemy zbudować kanalizację"},
            "zaprojektuj_i_wybuduj": {"wartosc": "tak", "cytat": "Wykonawca ma też zaprojektować sieć"},
            "zamawiajacy": {"wartosc": "Gmina Lipowo", "cytat": "gminie Lipowo"},
            "miejscowosc": {"wartosc": "", "cytat": ""},
            "wojewodztwo": {"wartosc": "warmińsko-mazurskie", "cytat": "województwie warmińsko-mazurskim"},
            "kwota": {"tekst": "6 mln zł", "vat": "brutto", "cytat": "około 6 mln zł brutto"},
            "czas_realizacji": {"liczba": "", "jednostka": "", "cytat": ""},
            "dlugosc_sieci_km": {"wartosc": "50", "cytat": "zmyślony cytat, którego nie ma"},   # zła liczba + fałszywy cytat
            "liczba_przylaczy": {"wartosc": "40", "cytat": "ze 40 przyłączy"},
            "elementy": ["przyłącza / sięgacze"],
            "slowa_kluczowe": ["kanalizacja sanitarna", "przyłącza kanalizacyjne"],
            "streszczenie": "Budowa ok. 5 km kanalizacji w gminie Lipowo.",
        }
        with mock.patch.object(llm, "enabled", return_value=True), mock.patch.object(llm, "understand_query", return_value=answer), \
                mock.patch.object(llm, "cfg", return_value={"model": "test-model"}):
            q = understand(self.conn, text)
        P = {k: v["value"] for k, v in q["params"].items()}
        self.assertEqual(P["subsector"], "kan_san")
        self.assertTrue(P["design_build"])                       # tylko model to wyczytał – cytat zweryfikowany
        self.assertEqual(q["params"]["design_build"]["source"], "model")
        self.assertEqual(P["budget"], 6_000_000)
        self.assertEqual(P["length_km"], 5.0)                    # reguły (5 km) wygrywają z błędną liczbą modelu
        self.assertIn("length_km", q["conflicts"])               # a rozbieżność jest pokazana użytkownikowi
        self.assertEqual(P["province"], "PL28")
        self.assertEqual(q["search_terms"][0], "kanalizacja sanitarna")
        self.assertEqual(q["llm_info"]["model"], "test-model")
        self.assertFalse(q["vague"])


class TestClaudeCodeProvider(unittest.TestCase):
    def test_structured_output(self):
        import json
        from types import SimpleNamespace
        from unittest import mock
        from riskminer import llm
        fake = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"is_error": False, "result": "{}", "structured_output": {"a": "b"}}))
        with mock.patch.object(llm, "cfg", return_value={"provider": "claude-code", "model": "claude-opus-5-5", "effort": "low", "timeout_s": 30}), \
                mock.patch.object(llm, "_claude_bin", return_value="/usr/bin/claude"), mock.patch("subprocess.run", return_value=fake) as run:
            out = llm.chat_json("system", "tekst", {"type": "object"})
        self.assertEqual(out, {"a": "b"})
        cmd = run.call_args[0][0]
        self.assertIn("--json-schema", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")           # bez narzędzi
        self.assertEqual(run.call_args.kwargs["input"], "tekst")     # opis przez stdin, nie w linii poleceń


class TestSectors(unittest.TestCase):
    def test_sector_from_text(self):
        from riskminer.query import sector_from_text
        cases = {
            "Przebudowa drogi gminnej wraz z kanalizacją deszczową w Brzezinach": "drogi",
            "Rozbudowa sieci kanalizacji sanitarnej; trasa przecina drogę wojewódzką nr 598": "wod-kan",
            "Termomodernizacja budynku szkoły podstawowej z pompą ciepła i fotowoltaiką": "kubatura",
            "Budowa instalacji fotowoltaicznych na 12 budynkach użyteczności publicznej": "energetyka",
            "Kolejny etap budowy wodociągu w gminie": "wod-kan",
            "Modernizacja przejazdu kolejowego i peronu": "kolej",
            "Remont mostu na rzece Wkrze": "drogi",
        }
        for text, sector in cases.items():
            self.assertEqual(sector_from_text(text)[0], sector, text)
