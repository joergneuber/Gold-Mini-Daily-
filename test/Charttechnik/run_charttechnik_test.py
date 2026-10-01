"""Isolierter Charttechnik-Testlauf.

Diagnose fuer EIN konkretes Gold-Setup:
Chartstrukturen -> charttechnische Entry-Grundlage -> Stop -> TP1 -> TP2 -> CRV.

Wichtig:
- Produktionslogik wird nicht ausgefuehrt/veraendert.
- Kein Score.
- Keine 2R-/3R-Fallbacks.
- Das CRV ist ausschliesslich Zulassungsschranke.
- Der aktuell laufende Intraday-Kanal (seit dem letzten relevanten Wendepunkt)
  wird separat vom Gesamtkanal diagnostiziert und ueber mehrere Zeitpunkte
  verfolgt, damit die dynamische Veraenderung des Kanals sichtbar wird.
"""
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "test" / "Charttechnik" / "mini_daily_gold.py"

spec = importlib.util.spec_from_file_location("mini_daily_gold_charttechnik_test", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _safe(value):
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass
    return value


def _channel_at(reihe):
    """Liest den aktuell aktiven Wendepunkt-Kanal am letzten Bar aus."""
    info = mod.kanal_seit_wendepunkt(reihe)
    if info is None:
        return {"status": "kein_aktiver_wendepunkt_kanal"}

    last_ts = reihe.index[-1]
    x = mod.mdates.date2num(last_ts)
    out = {
        "status": "aktiv",
        "start": str(info["start"]),
        "ende": str(last_ts),
        "typ": info["typ"],
        "bars": int(len(info["reihe"])),
    }
    if info["typ"] == "kanal":
        k = info["daten"]
        obere = float(k["obere_linie"][0] * x + k["obere_linie"][1])
        untere = float(k["untere_linie"][0] * x + k["untere_linie"][1])
        out.update({
            "formation": k.get("formation"),
            "obere_grenze": obere,
            "untere_grenze": untere,
            "kanalbreite": obere - untere,
            "obere_steigung": float(k["obere_linie"][0]),
            "untere_steigung": float(k["untere_linie"][0]),
        })
    else:
        steigung, achse = info["daten"]
        out.update({
            "formation": "Aufwärtstrend" if steigung > 0 else "Abwärtstrend",
            "trendwert": float(steigung * x + achse),
            "steigung": float(steigung),
        })
    return out


def _channel_evolution(reihe, checkpoints=12):
    """Zeigt, wie sich der aktive Wendepunkt-Kanal mit neuen Bars veraendert."""
    n = len(reihe)
    if n < 30:
        return []
    start = max(30, n - checkpoints * 2)
    indices = list(range(start, n, max(1, (n - start) // max(1, checkpoints - 1))))
    indices = indices[-checkpoints:]
    out = []
    for i in indices:
        sub = reihe.iloc[:i + 1]
        c = _channel_at(sub)
        c["zeitpunkt"] = str(sub.index[-1])
        c["kurs_close"] = float(sub["Close"].iloc[-1])
        out.append(c)
    return out


def _kandidat_view(k, entry, stop):
    preis = float(k["preis"])
    crv = (preis - entry) / (entry - stop) if preis > entry and entry > stop else None
    rw = k.get("rollenwechsel") or {}
    return {
        "preis": preis,
        "typ": k.get("typ"),
        "ebene": k.get("ebene"),
        "seite": k.get("seite"),
        "quelle": k.get("quelle"),
        "treffer": k.get("treffer", 0),
        "crv": crv,
        "rollenwechsel": rw or None,
    }


def _build_structure_chain(setup, intraday, daily, kurs):
    entry = float(setup.get("entry", kurs))
    stop = setup.get("stop")
    risiko = (entry - float(stop)) if stop is not None else None
    kandidaten = setup.get("kandidaten", [])

    supports = [k for k in kandidaten if k.get("seite") == "support" and float(k["preis"]) < entry]
    resistances = [k for k in kandidaten if k.get("seite") == "widerstand" and float(k["preis"]) > entry]

    def nearest(items):
        return sorted(items, key=lambda k: float(k["preis"]))

    stop_candidates = []
    for k in sorted(supports, key=lambda x: float(x["preis"]), reverse=True):
        v = _kandidat_view(k, entry, stop if stop is not None else entry)
        v["abstand_entry"] = entry - float(k["preis"])
        stop_candidates.append(v)

    tp_candidates = []
    for k in nearest(resistances):
        v = _kandidat_view(k, entry, stop if stop is not None else entry)
        if v["crv"] is not None:
            v["verworfen"] = v["crv"] <= 1.0
            v["verwerfungsgrund"] = "CRV <= 1" if v["verworfen"] else None
        else:
            v["verworfen"] = True
            v["verwerfungsgrund"] = "kein_gueltiges_risiko"
        tp_candidates.append(v)

    selected_tp1 = None
    selected_tp2 = None
    if setup.get("tp1") is not None:
        selected_tp1 = {
            "preis": setup["tp1"],
            "quelle": setup.get("tp1_quelle"),
            "crv": setup.get("tp1_crv"),
        }
    if setup.get("tp2") is not None:
        selected_tp2 = {
            "preis": setup["tp2"],
            "quelle": setup.get("tp2_quelle"),
            "crv": setup.get("tp2_crv"),
        }

    # Entry-Grundlage bewusst diagnostisch: kein kuenstlicher Triggerpreis.
    # Wir zeigen die naechsten charttechnischen Strukturen unter/ueber dem
    # aktuellen Kurs, aus denen der aktuelle Entry-Kontext begruendet werden kann.
    entry_supports = nearest([k for k in kandidaten if k.get("seite") == "support" and float(k["preis"]) <= entry])
    entry_resistances = nearest([k for k in kandidaten if k.get("seite") == "widerstand" and float(k["preis"]) >= entry])

    return {
        "kurs_und_entry": {
            "aktueller_kurs": kurs,
            "entry": entry,
            "entry_ist_aktueller_kurs": abs(entry - kurs) < 1e-9,
            "hinweis": "Der Test verschiebt den Entry nicht mathematisch; die charttechnische Grundlage wird separat ausgewiesen.",
        },
        "entry_chartgrundlage": {
            "supports_unter_oder_am_entry": [_kandidat_view(k, entry, stop if stop is not None else entry) for k in entry_supports[:8]],
            "widerstaende_ueber_oder_am_entry": [_kandidat_view(k, entry, stop if stop is not None else entry) for k in entry_resistances[:8]],
        },
        "stop": {
            "auswahl": setup.get("stop"),
            "quelle": setup.get("stop_quelle"),
            "risiko": risiko,
            "support_kandidaten_nach_naechster_lage": stop_candidates[:12],
        },
        "tp": {
            "alle_widerstands_und_rollenwechsel_kandidaten": tp_candidates,
            "tp1_auswahl": selected_tp1,
            "tp2_auswahl": selected_tp2,
            "regel": "Chartstruktur zuerst; CRV > 1 fuer TP1; TP2 nur naechste hoehere Struktur mit CRV >= 2; kein mathematischer 2R/3R-Fallback.",
        },
        "trade_entscheidung": {
            "status": setup.get("status"),
            "crv_tp1": setup.get("tp1_crv"),
            "crv_tp2": setup.get("tp2_crv"),
            "zulassung": "CRV > 1" if setup.get("tp1_crv") is not None and setup.get("tp1_crv") > 1 else "verwerfen",
        },
    }


def main():
    if not os.environ.get("TWELVEDATA_API_KEY"):
        raise SystemExit("TWELVEDATA_API_KEY fehlt – echter Gold-Test kann nicht ausgefuehrt werden.")

    daten = mod.hole_kursdaten()
    intraday = daten["intraday_reihe"]
    daily = mod.hole_langfrist_daten(monate=36)
    if daily is None or daily.empty:
        raise SystemExit("Keine ausreichenden Tagesdaten fuer den Test erhalten.")

    kurs = float(daten["realtime"])
    zeitpunkt = intraday.index[-1]
    setup = mod.bestimme_chart_setup(
        zeitpunkt,
        intraday_reihe=intraday,
        daily_reihe=daily,
        aktueller_kurs=kurs,
    )

    print("=== CHARTTECHNIK-TEST: EIN GOLD-SETUP ===")
    print(f"Kurs:     {kurs:.4f}")
    print(f"Intraday: {intraday.index[-1]} | Bars: {len(intraday)}")
    print(f"Daily:    {daily.index[-1]} | Bars: {len(daily)}")

    structure_chain = _build_structure_chain(setup, intraday, daily, kurs)

    kanal_gesamt = mod.diagnostiziere_kanalvarianten(
        intraday, mod.INTRADAY_KANAL_FENSTER, mod.INTRADAY_KANAL_MIN_PUNKTE
    )
    kanal_aktuell = _channel_at(intraday)
    kanal_verlauf = _channel_evolution(intraday, checkpoints=12)

    rollenwechsel = []
    for k in setup.get("kandidaten", []):
        rw = k.get("rollenwechsel")
        if rw:
            rollenwechsel.append({
                "preis": k.get("preis"),
                "ebene": k.get("ebene"),
                "typ": k.get("typ"),
                "urspruengliche_seite": rw.get("urspruengliche_seite"),
                "neue_seite": rw.get("neue_seite"),
                "quelle": k.get("quelle"),
                "support_oder_widerstand_zeit": rw.get("support_zeit", rw.get("widerstand_zeit")),
                "bruch_zeit": rw.get("bruch_zeit"),
                "retest_zeit": rw.get("retest_zeit"),
                "bestaetigungs_bars": rw.get("bestaetigungs_bars"),
            })

    print("\n=== VOLLSTAENDIGE STRUKTURKETTE ===")
    print(json.dumps(_safe(structure_chain), ensure_ascii=False, indent=2, default=str))
    print("\n=== AKTUELLER WENDEPUNKT-KANAL ===")
    print(json.dumps(_safe(kanal_aktuell), ensure_ascii=False, indent=2, default=str))
    print("\n=== KANALVERLAUF: LETZTE ENTWICKLUNG ===")
    print(json.dumps(_safe(kanal_verlauf), ensure_ascii=False, indent=2, default=str))
    print("\n=== GESAMTKANAL CLOSE vs HIGH/LOW ===")
    print(json.dumps(_safe(kanal_gesamt), ensure_ascii=False, indent=2, default=str))
    print("\n=== SUPPORT/WIDERSTAND-ROLLENWECHSEL ===")
    print(json.dumps(_safe(rollenwechsel), ensure_ascii=False, indent=2, default=str))

    out = {
        "realtime": kurs,
        "intraday_end": str(intraday.index[-1]),
        "daily_end": str(daily.index[-1]),
        "chart_setup": _safe(setup),
        "strukturkette": _safe(structure_chain),
        "aktiver_wendepunkt_kanal": _safe(kanal_aktuell),
        "kanal_verlauf": _safe(kanal_verlauf),
        "kanal_gesamt_diagnose": _safe(kanal_gesamt),
        "rollenwechsel_diagnose": _safe(rollenwechsel),
    }
    Path("charttechnik_test_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("\nDiagnose gespeichert: charttechnik_test_result.json")


if __name__ == "__main__":
    main()
