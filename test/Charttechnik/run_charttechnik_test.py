"""Isolierter Diagnoselauf fuer die Charttechnik-Testversion.

Keine Trade-Alerts, keine Mails, keine Produktionsdateien.
Verwendet die Testversion aus test/Charttechnik/mini_daily_gold.py und
laedt die echten Gold-Daten ueber Twelve Data.

Der TP-Teil ist rein diagnostisch: Die bestehende
bestimme_strukturelle_tps()-Logik wird nicht veraendert. Zusaetzlich werden
alle von ihr gelieferten charttechnischen Kandidaten einzeln ausgegeben und
nachvollziehbar als verworfen/TP1/TP2/sonstiger Kandidat klassifiziert.
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


def _diagnose_tp(tp_info):
    """Erklaert die bereits von bestimme_strukturelle_tps() getroffene Auswahl.

    Diese Funktion berechnet keine neuen TP-Kandidaten und veraendert keine
    TP-Regel. Sie wertet nur die von der bestehenden TP-Funktion gelieferten
    Listen aus. Damit wird sichtbar, ob z.B. ein charttechnisches Ziel bei
    4.232 USD ueberhaupt als Kandidat ankommt.
    """
    kandidaten = list(tp_info.get("kandidaten") or [])
    verworfen = list(tp_info.get("verworfen") or [])
    tp1 = tp_info.get("tp1")
    tp2 = tp_info.get("tp2")

    tp1_candidate = None
    tp2_candidate = None
    if tp1 is not None:
        tp1_candidate = next(
            (k for k in kandidaten if abs(float(k.get("preis", 0)) - float(tp1)) < 1e-7),
            None,
        )
    if tp2 is not None:
        tp2_candidate = next(
            (k for k in kandidaten if abs(float(k.get("preis", 0)) - float(tp2)) < 1e-7),
            None,
        )

    def structure_priority(k):
        typ = str(k.get("typ", "")).lower()
        if typ == "widerstandszone":
            return (3, int(k.get("treffer", 0) or 0))
        if typ in ("umkehrzone", "swing", "umkehr"):
            return (2, 0)
        return (1, 0)

    def level_priority(k):
        return {"Intraday": 1, "Tageschart": 2, "6M": 3}.get(k.get("ebene"), 0)

    valid = [k for k in kandidaten if float(k.get("crv", 0)) > 1.0]
    valid_sorted = sorted(
        valid,
        key=lambda k: (structure_priority(k), level_priority(k), -float(k["preis"])),
        reverse=True,
    )
    # Die TP-Funktion verwendet max() mit exakt demselben Schluessel.
    # Dadurch ist diese Reihenfolge nur eine lesbare Diagnose der bestehenden
    # Auswahl und keine zweite, abweichende Auswahlregel.

    details = []
    selected_prices = set()
    if tp1_candidate:
        selected_prices.add(("TP1", float(tp1_candidate["preis"])))
    if tp2_candidate:
        selected_prices.add(("TP2", float(tp2_candidate["preis"])))

    for k in verworfen:
        row = dict(k)
        row["entscheidung"] = "VERWORFEN"
        row["begruendung"] = "CRV <= 1"
        details.append(row)

    for k in kandidaten:
        row = dict(k)
        preis = float(k["preis"])
        if tp1_candidate is k or (tp1_candidate and abs(preis - float(tp1_candidate["preis"])) < 1e-7 and k.get("quelle") == tp1_candidate.get("quelle")):
            row["entscheidung"] = "TP1"
            row["begruendung"] = "beste charttechnische Prioritaet nach bestehender Hierarchie"
        elif tp2_candidate is k or (tp2_candidate and abs(preis - float(tp2_candidate["preis"])) < 1e-7 and k.get("quelle") == tp2_candidate.get("quelle")):
            row["entscheidung"] = "TP2"
            row["begruendung"] = "naechsthoehere charttechnische Struktur oberhalb TP1"
        else:
            row["entscheidung"] = "NICHT GEWAEHLT"
            if tp1_candidate is not None and preis <= float(tp1_candidate["preis"]):
                row["begruendung"] = "gueltiger Kandidat, aber nicht oberhalb des gewaehlten TP1"
            elif tp1_candidate is not None:
                row["begruendung"] = "gueltiger Kandidat; TP1 hat nach der bestehenden Struktur-/Zeitebenen-Hierarchie Vorrang"
            else:
                row["begruendung"] = "kein gueltiger TP1-Kandidat vorhanden"
        details.append(row)

    ziel_4232 = []
    for k in verworfen + kandidaten:
        if abs(float(k.get("preis", 0)) - 4232.0) <= 3.0:
            ziel_4232.append(dict(k))

    return {
        "ziel_4232_diagnose": ziel_4232,
        "anzahl_gueltige_kandidaten": len(kandidaten),
        "anzahl_verworfene_kandidaten": len(verworfen),
        "tp1": tp1_candidate,
        "tp2": tp2_candidate,
        "kandidaten_nach_bestehender_hierarchie": valid_sorted,
        "details": details,
    }


def main():
    if not os.environ.get("TWELVEDATA_API_KEY"):
        raise SystemExit("TWELVEDATA_API_KEY fehlt – echter Gold-Test kann nicht ausgefuehrt werden.")

    daten = mod.hole_kursdaten()
    intraday = daten["intraday_reihe"]
    daily = mod.hole_langfrist_daten(monate=36)
    if daily is None or daily.empty:
        raise SystemExit("Keine ausreichenden Tagesdaten fuer den Test erhalten.")

    print("=== CHARTTECHNIK-TEST: ECHTE GOLD-DATEN ===")
    print(f"Realtime: {daten['realtime']:.4f}")
    print(f"Intraday: {intraday.index[-1]} | Bars: {len(intraday)}")
    print(f"Daily:    {daily.index[-1]} | Bars: {len(daily)}")

    position = mod.berechne_positionstrading_status(intraday_reihe=intraday)
    range_status = mod.berechne_range_ausbruch_status(daily_lang=daily)

    print("\n=== POSITIONSTRADING ===")
    print(json.dumps(_safe(position), ensure_ascii=False, indent=2, default=str))
    print("\n=== RANGE-AUSBRUCH ===")
    print(json.dumps(_safe(range_status), ensure_ascii=False, indent=2, default=str))

    tp_diagnose = None
    if position.get("status") == "offen":
        entry = position.get("einstieg")
        stop = position.get("stop")
        entry_zeit = position.get("einstieg_datum")
        if entry is not None and stop is not None and entry_zeit is not None:
            tp_info = mod.bestimme_strukturelle_tps(
                entry=float(entry),
                stop=float(stop),
                entry_zeit=entry_zeit,
                intraday_reihe=intraday,
                daily_reihe=daily,
            )
            tp_diagnose = _diagnose_tp(tp_info)
            tp_diagnose["roh_tp_info"] = _safe(tp_info)

            print("\n=== TP-DIAGNOSE: ALLE CHARTTECHNISCHEN KANDIDATEN ===")
            print(json.dumps(_safe(tp_diagnose), ensure_ascii=False, indent=2, default=str))

            print("\n=== KURZLISTE ===")
            for item in tp_diagnose["details"]:
                print(
                    f"{item.get('entscheidung'):15} "
                    f"{float(item.get('preis', 0)):10.2f} | "
                    f"{item.get('ebene', '-'):11} | "
                    f"{item.get('typ', '-'):16} | "
                    f"CRV {float(item.get('crv', 0)):.2f} | "
                    f"{item.get('quelle', '-')} | {item.get('begruendung', '')}"
                )

    out = {
        "realtime": float(daten["realtime"]),
        "intraday_end": str(intraday.index[-1]),
        "daily_end": str(daily.index[-1]),
        "positionstrading": _safe(position),
        "range_ausbruch": _safe(range_status),
        "tp_diagnose": _safe(tp_diagnose),
    }
    Path("charttechnik_test_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("\nDiagnose gespeichert: charttechnik_test_result.json")


if __name__ == "__main__":
    main()
