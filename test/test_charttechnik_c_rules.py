import sys
import types

try:
    from google import genai  # noqa: F401
except Exception:
    google_pkg = types.ModuleType("google")
    genai_mod = types.ModuleType("google.genai")
    google_pkg.genai = genai_mod
    sys.modules.setdefault("google", google_pkg)
    sys.modules["google.genai"] = genai_mod

if "economic_events" not in sys.modules:
    economic_events_mod = types.ModuleType("economic_events")
    economic_events_mod.briefing_block = lambda days_ahead=7: ("", [])
    sys.modules["economic_events"] = economic_events_mod

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "test" / "Charttechnik" / "run_charttechnik_test.py"

spec = importlib.util.spec_from_file_location("charttechnik_runner_rules_test", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def kandidat(preis, crv_role=True, rolle=True):
    return {
        "preis": float(preis),
        "typ": "widerstandszone",
        "seite": "widerstand",
        "ebene": "Intraday",
        "quelle": "synthetische Teststruktur",
        "treffer": 3,
        "aktuelle_rolle_gueltig": rolle,
    }


class CharttechnikCRulesTest(unittest.TestCase):
    def test_tp2_darf_nicht_uebersprungen_werden(self):
        entry = 4164.76
        stop = 4147.99
        # 4186.11 -> 1.27R, 4192.16 -> 1.63R, 4206.81 -> 2.51R
        kandidaten = [
            kandidat(4186.11),
            kandidat(4192.16),
            kandidat(4206.81),
        ]
        result = runner._ermittle_tp_kette(kandidaten, entry, stop)
        self.assertIsNotNone(result["tp1"])
        self.assertAlmostEqual(result["tp1"]["preis"], 4186.11, places=6)
        self.assertIsNone(result["tp2"])
        self.assertEqual(result["tp2_pruefung"]["status"], "nicht_vorhanden")
        self.assertFalse(result["tp2_pruefung"]["spaetere_strukturen_geprueft"])

    def test_tp2_wird_genommen_wenn_direkt_naechstes_ziel_geeignet_ist(self):
        entry = 4164.76
        stop = 4147.99
        kandidaten = [kandidat(4186.11), kandidat(4206.81)]
        result = runner._ermittle_tp_kette(kandidaten, entry, stop)
        self.assertAlmostEqual(result["tp1"]["preis"], 4186.11, places=6)
        self.assertAlmostEqual(result["tp2"]["preis"], 4206.81, places=6)

    def test_drei_punkte_zone(self):
        kandidaten = [
            kandidat(4185.0),
            kandidat(4187.5),
            kandidat(4191.0),
        ]
        z = runner._konsolidiere_nahe_zielstrukturen(kandidaten, toleranz=3.0)
        self.assertEqual(len(z), 2)
        self.assertEqual(sorted(z[0]["zusammengefasste_preise"]), [4185.0, 4187.5])

    def test_support_unterkante_wird_als_stop_verwendet(self):
        stop, status = runner._stop_aus_support({
            "preis": 4148.2458,
            "untere_grenze": 4141.80,
        })
        self.assertEqual(status, "bestimmbar")
        self.assertAlmostEqual(stop, 4141.80, places=6)

    def test_chartrolle_ungueltig_bedeutet_keine_crv_pruefung(self):
        kandidaten = [kandidat(4186.11, rolle=False)]
        rows = runner._tp_kandidaten_diagnose(kandidaten, 4164.76, 4147.99)
        self.assertIsNone(rows[0]["crv"])
        self.assertTrue(rows[0]["verworfen"])
        self.assertEqual(rows[0]["verwerfungsgrund"], "kein_gueltiger_charttechnischer_grund")


if __name__ == "__main__":
    unittest.main()
