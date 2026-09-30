"""Isolierter Diagnoselauf fuer die Charttechnik-Testversion.

Keine Trade-Alerts, keine Mails, keine Produktionsdateien.
Verwendet die Testversion aus test/charttechnik/mini_daily_gold.py und
laedt die echten Gold-Daten ueber Twelve Data.
"""
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "test" / "charttechnik" / "mini_daily_gold.py"

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

    print("=== CHARTTECHNIK-TEST: ECHTE GOLD-DATEN ===")
    print(f"Realtime: {daten['realtime']:.4f}")
    print(f"Intraday: {intraday.index[-1]} | Bars: {len(intraday)}")
    print(f"Daily:    {daily.index[-1]} | Bars: {len(daily)}")

    # Bestehende Signalerzeugung nur diagnostisch ausfuehren.
    # Es werden weder Alerts geschrieben noch versendet.
    position = mod.berechne_positionstrading_status(intraday_reihe=intraday)
    range_status = mod.berechne_range_ausbruch_status(daily_lang=daily)

    print("\n=== POSITIONSTRADING ===")
    print(json.dumps(_safe(position), ensure_ascii=False, indent=2, default=str))
    print("\n=== RANGE-AUSBRUCH ===")
    print(json.dumps(_safe(range_status), ensure_ascii=False, indent=2, default=str))

    out = {
        "realtime": float(daten["realtime"]),
        "intraday_end": str(intraday.index[-1]),
        "daily_end": str(daily.index[-1]),
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
