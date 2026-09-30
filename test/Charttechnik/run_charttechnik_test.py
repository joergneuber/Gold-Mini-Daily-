"""Isolierter Charttechnik-Testlauf.

Keine Trade-Alerts, keine Mails und keine Produktionsdateien.
Die Testlogik bestimmt Chart-Setup, Entry, Stop, TP1 und TP2 zuerst aus
charttechnischen Strukturen und prueft das CRV erst danach.
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

    print("=== CHARTTECHNIK-TEST: ECHTE GOLD-DATEN ===")
    print(f"Kurs:     {kurs:.4f}")
    print(f"Intraday: {intraday.index[-1]} | Bars: {len(intraday)}")
    print(f"Daily:    {daily.index[-1]} | Bars: {len(daily)}")
    print("\n=== CHART-SETUP ===")
    print(json.dumps(_safe(setup), ensure_ascii=False, indent=2, default=str))

    # Die beiden bestehenden Signalerzeuger werden nur diagnostisch ausgefuehrt.
    # Es werden keine Alerts geschrieben/versendet und keine Produktionsdateien veraendert.
    position = mod.berechne_positionstrading_status(intraday_reihe=intraday)
    range_status = mod.berechne_range_ausbruch_status(daily_lang=daily)

    print("\n=== POSITIONSTRADING ===")
    print(json.dumps(_safe(position), ensure_ascii=False, indent=2, default=str))
    print("\n=== RANGE-AUSBRUCH ===")
    print(json.dumps(_safe(range_status), ensure_ascii=False, indent=2, default=str))

    out = {
        "realtime": kurs,
        "intraday_end": str(intraday.index[-1]),
        "daily_end": str(daily.index[-1]),
        "chart_setup": _safe(setup),
        "positionstrading": _safe(position),
        "range_ausbruch": _safe(range_status),
    }
    Path("charttechnik_test_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("\nDiagnose gespeichert: charttechnik_test_result.json")


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


if __name__ == "__main__":
    main()
