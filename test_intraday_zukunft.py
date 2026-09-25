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
from mini_daily_gold import analysiere_intraday_zukunft, formatiere_intraday_zukunft, analysiere_intraday_chartstruktur, finde_trendkanal

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
        "intraday_reihe": make_frame("h", 250, drift=0.50),
        "intraday_30m": make_frame("30min", 250, drift=0.25),
        "intraday_15m": make_frame("15min", 250, drift=0.12),
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
    assert result["bull_trigger"] == 4589.82
    assert result["bear_trigger"] == 4391.32
    print(formatiere_intraday_zukunft(result, lambda x: f"{x:.2f}"))
    print("SMOKE TEST: OK")


def test_chartstruktur_wird_aus_der_bestehenden_intraday_kanalberechnung_uebernommen():
    idx = pd.date_range("2026-09-01", periods=160, freq="h", tz="UTC")
    t = np.arange(160)
    close = 4400.0 - 0.55 * t + 18.0 * np.sin(t * np.pi / 5.0)
    df = pd.DataFrame({
        "Open": close - 1.0,
        "High": close + 2.0,
        "Low": close - 2.0,
        "Close": close,
    }, index=idx)

    kanal = finde_trendkanal(df, fenster=3, min_punkte=2)
    assert kanal is not None
    assert kanal["formation"] == "Abwärtskanal"

    struktur = analysiere_intraday_chartstruktur(df)
    assert struktur is not None
    assert struktur["formation"] == "Abwärtskanal"
    assert "Kurs innerhalb der Formation" in struktur["text"]
    assert struktur["kanal"] is not None
    assert struktur["kanal"]["obere_grenze"] > struktur["kanal"]["untere_grenze"]
    assert struktur["kanal"]["kanalbreite"] > 0
    assert struktur["kanal"]["abstand_oben"] > 0
    assert struktur["kanal"]["abstand_unten"] > 0
    assert 0 <= struktur["kanal"]["position_im_kanal_pct"] <= 100
    assert "obere Begrenzung" in struktur["text"]
    assert "untere Begrenzung" in struktur["text"]
    assert "Abstand oben" in struktur["text"]
    assert "Abstand unten" in struktur["text"]
    assert "Position im Kanal" in struktur["text"]

    daten = {
        "realtime": float(close[-1]),
        "intraday_reihe": df,
        "intraday_30m": make_frame("30min", 250, drift=0.25),
        "intraday_15m": make_frame("15min", 250, drift=0.12),
    }
    szenarien = {
        "naechster_widerstand": 4589.82,
        "ziel_bullisch": 4656.40,
        "naechster_support": 4391.32,
        "ziel_baerisch": 4259.40,
    }
    result = analysiere_intraday_zukunft(
        daten, szenarien, intraday_chartstruktur=struktur
    )
    text = formatiere_intraday_zukunft(result, lambda x: f"{x:.2f}")
    assert "Chartstruktur: Abwärtskanal" in text
    assert "obere Begrenzung" in text
    assert "untere Begrenzung" in text
    assert "Abstand oben" in text
    assert "Abstand unten" in text
    assert "Position im Kanal" in text
    assert "MTF-Score" not in text
    assert "Gesamtbild:" not in text

if __name__ == "__main__":
    main()
