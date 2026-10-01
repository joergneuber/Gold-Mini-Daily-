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


def _rollenwechsel_aktuell_gueltig(kandidat, aktueller_kurs):
    """Prueft nur die aktuelle Rolle eines bereits bestaetigten Rollenwechsels.

    Das ist bewusst keine neue Rollenwechsel-Erkennung. Sie verhindert nur, dass
    ein historischer S->R/R->S-Wechsel weiterhin als aktive Struktur behandelt
    wird, obwohl der aktuelle Schlusskurs die neue Rolle inzwischen wieder gebrochen
    hat.
    """
    rw = kandidat.get("rollenwechsel") or {}
    neue_seite = rw.get("neue_seite")
    if not neue_seite:
        return True, "keine_rollenwechsel_pruefung_erforderlich"
    preis = float(kandidat["preis"])
    kurs = float(aktueller_kurs)
    if neue_seite == "support":
        if kurs >= preis:
            return True, "R->S weiterhin oberhalb/auf Support"
        return False, "R->S inzwischen unterhalb des Support-Levels gebrochen"
    if neue_seite == "widerstand":
        if kurs <= preis:
            return True, "S->R weiterhin unterhalb/auf Widerstand"
        return False, "S->R inzwischen oberhalb des Widerstands gebrochen"
    return True, "unbekannte_rollenwechselrolle_nicht_automatisch_verworfen"


def _aktive_kandidaten(kandidaten, aktueller_kurs):
    """Filtert Kandidaten nur nach aktuell gueltiger Chartrolle; kein Score."""
    out = []
    diagnostik = []
    for k in kandidaten:
        gueltig, grund = _rollenwechsel_aktuell_gueltig(k, aktueller_kurs)
        v = dict(k)
        v["aktuelle_rolle_gueltig"] = gueltig
        v["rollenwechsel_pruefung"] = grund
        diagnostik.append({
            "preis": float(k["preis"]),
            "typ": k.get("typ"),
            "ebene": k.get("ebene"),
            "urspruengliche_seite": k.get("seite"),
            "aktuelle_rolle_gueltig": gueltig,
            "pruefung": grund,
        })
        if gueltig:
            if (k.get("rollenwechsel") or {}).get("neue_seite"):
                v["seite"] = (k.get("rollenwechsel") or {}).get("neue_seite")
            out.append(v)
    return out, diagnostik


def _aktive_wendepunkt_kandidaten(reihe):
    """Erzeugt aus dem *aktuellen* Wendepunkt-Kanal zwei testweise Chartkandidaten."""
    info = mod.kanal_seit_wendepunkt(reihe)
    if info is None or info.get("typ") != "kanal":
        return [], {"status": "kein_aktiver_kanal"}
    x = mod.mdates.date2num(reihe.index[-1])
    k = info["daten"]
    oben = float(k["obere_linie"][0] * x + k["obere_linie"][1])
    unten = float(k["untere_linie"][0] * x + k["untere_linie"][1])
    ebene = "Intraday"
    formation = k.get("formation", info.get("typ"))
    kandidaten = [
        {
            "preis": oben, "typ": "aktiver_kanal", "seite": "widerstand",
            "ebene": ebene, "quelle": f"Aktiver Wendepunkt-Kanal ({formation})",
            "treffer": 0, "dynamisch": True,
        },
        {
            "preis": unten, "typ": "aktiver_kanal", "seite": "support",
            "ebene": ebene, "quelle": f"Aktiver Wendepunkt-Kanal ({formation})",
            "treffer": 0, "dynamisch": True,
        },
    ]
    return kandidaten, {
        "status": "aktiv",
        "start": str(info["start"]),
        "ende": str(reihe.index[-1]),
        "formation": formation,
        "obere_grenze": oben,
        "untere_grenze": unten,
    }


def _bounce_confirmation(reihe, support_price, lookback=5):
    """Prueft einen abgeschlossenen Support-Bounce ohne Look-ahead."""
    if reihe is None or len(reihe) < 3:
        return {"bestaetigt": False, "grund": "zu_wenig_bars"}
    start = max(0, len(reihe) - lookback)
    teil = reihe.iloc[start:]
    touched = []
    for i in range(len(teil) - 1):
        if float(teil["Low"].iloc[i]) <= float(support_price):
            touched.append(i)
    if not touched:
        return {"bestaetigt": False, "grund": "kein_abgeschlossener_touch_des_supports"}
    touch_i = touched[-1]
    for j in range(touch_i + 1, len(teil)):
        if float(teil["Close"].iloc[j]) > float(support_price):
            return {
                "bestaetigt": True,
                "touch_zeit": str(teil.index[touch_i]),
                "bestaetigungs_zeit": str(teil.index[j]),
                "bestaetigungs_close": float(teil["Close"].iloc[j]),
                "regel": "Low beruehrt/unterschreitet Support; spaeterer abgeschlossener Close schliesst wieder darueber.",
            }
    return {"bestaetigt": False, "grund": "touch_ohne_bestaetigungs_close_darueber"}


def _build_structure_chain(setup, intraday, daily, kurs, kanal_aktuell=None):
    """Baut die Test-Variante der vollstaendigen Chartkette.

    Wichtig: Diese Funktion aendert NICHT die Produktionsfunktion bestimme_chart_setup.
    Sie ersetzt im Test lediglich die bisherige statische/alte Kanalbetrachtung durch
    die aktuell gueltigen Strukturen und prueft Entry/Stop/TP1/TP2 als zusammenhaengende
    Chartkette. CRV ist nur Zulassungsschranke, niemals Score oder Auswahlgewicht.
    """
    basis = list(setup.get("kandidaten", []))
    aktive, rollen_diag = _aktive_kandidaten(basis, kurs)
    dynamisch, kanal_diag = _aktive_wendepunkt_kandidaten(intraday)
    kandidaten = aktive + dynamisch

    # Keine doppelte Struktur, wenn der dynamische Kanal bereits als identisches Level
    # in der Kandidatenliste vorhanden ist.
    dedup = {}
    for k in kandidaten:
        key = (round(float(k["preis"]), 4), k.get("seite"), k.get("ebene"))
        dedup[key] = k
    kandidaten = list(dedup.values())

    supports = sorted(
        [k for k in kandidaten if k.get("seite") == "support" and float(k["preis"]) < kurs],
        key=lambda k: float(k["preis"]), reverse=True,
    )
    resistances = sorted(
        [k for k in kandidaten if k.get("seite") == "widerstand" and float(k["preis"]) > kurs],
        key=lambda k: float(k["preis"]),
    )

    # ENTRY: kein willkuerlicher Preis. Der aktuelle abgeschlossene Close ist nur dann
    # ein Test-Entry, wenn eine darunterliegende Chartstruktur in den letzten 5 Bars
    # beruehrt und danach per Close zurueckerobert wurde.
    entry_candidates = []
    for support in supports:
        bounce = _bounce_confirmation(intraday, float(support["preis"]), lookback=5)
        entry_candidates.append({
            "preis": float(support["preis"]),
            "quelle": support.get("quelle"),
            "ebene": support.get("ebene"),
            "typ": support.get("typ"),
            "rollenwechsel": support.get("rollenwechsel"),
            "bounce": bounce,
        })
    bestaetigte_entries = [x for x in entry_candidates if x["bounce"].get("bestaetigt")]
    entry_basis = bestaetigte_entries[0] if bestaetigte_entries else None
    test_entry = float(entry_basis["bounce"]["bestaetigungs_close"]) if entry_basis else None

    stop_candidates = []
    if test_entry is not None:
        for support in sorted(
            [k for k in kandidaten if k.get("seite") == "support" and float(k["preis"]) < test_entry],
            key=lambda k: float(k["preis"]), reverse=True,
        ):
            stop_candidates.append({
                "preis": float(support["preis"]),
                "quelle": support.get("quelle"),
                "ebene": support.get("ebene"),
                "typ": support.get("typ"),
                "rollenwechsel": support.get("rollenwechsel"),
                "begruendung": "naechste aktuell gueltige Supportstruktur unter dem bestaetigten Entry",
            })

    stop_basis = stop_candidates[0] if stop_candidates else None
    stop = float(stop_basis["preis"]) if stop_basis else None
    risiko = (test_entry - stop) if test_entry is not None and stop is not None else None

    tp_candidates = []
    if test_entry is not None and risiko and risiko > 0:
        for r in sorted(
            [k for k in kandidaten if k.get("seite") == "widerstand" and float(k["preis"]) > test_entry],
            key=lambda k: float(k["preis"]),
        ):
            preis = float(r["preis"])
            crv = (preis - test_entry) / risiko
            row = _kandidat_view(r, test_entry, stop)
            row["aktuelle_rolle_gueltig"] = True
            row["crv"] = crv
            row["verworfen"] = crv <= 1.0
            row["verwerfungsgrund"] = "CRV <= 1" if crv <= 1.0 else None
            tp_candidates.append(row)

    gueltig_tp1 = [x for x in tp_candidates if not x["verworfen"]]
    tp1 = gueltig_tp1[0] if gueltig_tp1 else None
    tp2 = None
    if tp1 is not None:
        tp2_candidates = [x for x in gueltig_tp1 if x["preis"] > tp1["preis"] + 1e-6 and x["crv"] >= 2.0]
        tp2 = tp2_candidates[0] if tp2_candidates else None
    else:
        tp2_candidates = []

    return {
        "kurs": kurs,
        "entry": {
            "test_entry": test_entry,
            "status": "charttechnisch_bestaetigt" if entry_basis else "kein_bestaetigter_entry",
            "basis_support": entry_basis,
            "alle_entry_support_kandidaten": entry_candidates,
            "regel": "Entry nur nach bestaetigtem Bounce an einer aktuell gueltigen Supportstruktur; sonst kein kuenstlicher Entry.",
        },
        "stop": {
            "test_stop": stop,
            "basis": stop_basis,
            "alle_stop_support_kandidaten": stop_candidates,
            "risiko": risiko,
            "regel": "Stop auf/unter der naechsten aktuell gueltigen Supportstruktur unter dem bestaetigten Entry; ein konkreter Unter-der-Zone-Puffer wird noch nicht erfunden.",
        },
        "tp": {
            "tp1": tp1,
            "tp2": tp2,
            "alle_tp_kandidaten": tp_candidates,
            "regel": "Naechste charttechnische Widerstandshuerde oberhalb Entry; CRV > 1 ist nur Zulassung. TP2 = naechste hoehere Huerde mit CRV >= 2. Kein Score, kein 2R/3R-Fallback.",
        },
        "rollenwechsel": {
            "kandidaten": rollen_diag,
            "regel": "Historischer Rollenwechsel wird nur verwendet, solange die neue Rolle durch den aktuellen Schlusskurs nicht wieder gebrochen wurde.",
        },
        "aktiver_kanal": kanal_diag,
        "trade": {
            "status": "trade_zulaessig" if tp1 is not None else "verwerfen",
            "crv_tp1": tp1["crv"] if tp1 else None,
            "crv_tp2": tp2["crv"] if tp2 else None,
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
