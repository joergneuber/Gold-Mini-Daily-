"""Offline smoke test für die neue Daytrading-MTF-Zukunftsanalyse.
Erzeugt keine Produktionsdateien und benötigt keinen API-Key.
"""
import sys, types
import numpy as np
import pandas as pd
# Produktionsimport benötigt google.genai; im Offline-Smoke-Test nur stubben.
google_mod = types.ModuleType("google")
genai_mod = types.ModuleType("google.genai")
google_mod.genai = genai_mod
sys.modules.setdefault("google", google_mod)
sys.modules.setdefault("google.genai", genai_mod)
from mini_daily_gold import analysiere_intraday_zukunft, formatiere_intraday_zukunft, _ma_struktur

def make_frame(freq, bars, start=4400.0, drift=0.35):
    idx = pd.date_range("2026-08-19", periods=bars, freq=freq, tz="UTC")
    close = start + np.arange(bars) * drift + np.sin(np.arange(bars) / 3.0) * 2.0
    return pd.DataFrame({
        "Open": close - 1.0,
        "High": close + 3.0,
        "Low": close - 3.0,
        "Close": close,
    }, index=idx)

def main():
    daten = {
        "realtime": 4487.73,
        "intraday_reihe": make_frame("h", 300, drift=0.50),
        "intraday_30m": make_frame("30min", 300, drift=0.25),
        "intraday_15m": make_frame("15min", 300, drift=0.12),
    }
    szenarien = {
        "naechster_widerstand": 4589.82,
        "ziel_bullisch": 4656.40,
        "naechster_support": 4391.32,
        "ziel_baerisch": 4259.40,
    }
    result = analysiere_intraday_zukunft(daten, szenarien)
    assert result["status"] == "ok"
    assert set(result["frames"]) == {"1h", "30m", "15m"}
    for frame in result["frames"].values():
        for key in ("ema20", "ema50", "ema100", "ema200", "wma200"):
            assert frame[key] is not None
    assert result["bull_trigger"] == 4589.82
    assert result["bear_trigger"] == 4391.32
    erwarteter_widerstand = max(result["frames"]["30m"]["high"], result["frames"]["15m"]["high"])
    erwarteter_support = min(result["frames"]["30m"]["low"], result["frames"]["15m"]["low"])
    assert result["daytrade_resistance"] == erwarteter_widerstand
    assert result["daytrade_support"] == erwarteter_support
    print(formatiere_intraday_zukunft(result, lambda x: f"{x:.2f}"))
    print(f"REPORT-RANGE TEST: Long > {result['daytrade_resistance']:.2f} | Short < {result['daytrade_support']:.2f} | OK")
    daily = make_frame("D", 300, start=4400.0, drift=0.8)
    daily_ma = _ma_struktur(daily)
    assert daily_ma is not None
    for key in ("ema20", "ema50", "ema100", "ema200", "wma200"):
        assert daily_ma[key] is not None
    assert daily_ma["wma200"] != daily_ma["ema200"]
    print(f"DAILY MA TEST: EMA20={daily_ma["ema20"]:.2f} EMA50={daily_ma["ema50"]:.2f} EMA100={daily_ma["ema100"]:.2f} EMA200={daily_ma["ema200"]:.2f} WMA200={daily_ma["wma200"]:.2f} | OK")
    print("SMOKE TEST: OK")

if __name__ == "__main__":
    main()
