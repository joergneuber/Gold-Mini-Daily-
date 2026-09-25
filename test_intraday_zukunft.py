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
import mini_daily_gold as mdg
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
    assert struktur["kanal"]["kurs"] == float(df["Close"].iloc[-1])
    assert struktur["kanal"]["kurs_zeitpunkt"] == df.index[-1]
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
    assert "Kanalbezug: 1h-Schlusskurs" in text
    assert "MTF-Score" not in text
    assert "Gesamtbild:" not in text


def test_zukunftseinschaetzung_trennt_szenario_trigger_und_kanalausbruch(monkeypatch):
    class FakeResponse:
        text = "Intraday: Test. Bestätigung: Test. Daytrading: Test. Übergeordnet: Test."

    class FakeModels:
        def __init__(self):
            self.prompt = None

        def generate_content(self, model, contents):
            self.prompt = contents
            return FakeResponse()

    fake_client = types.SimpleNamespace(models=FakeModels())
    monkeypatch.setattr(mdg.genai, "Client", lambda api_key: fake_client, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    chartstruktur = {
        "text": "Abwärtskanal | Kurs innerhalb der Formation",
        "kurs": 4301.80,
        "kurs_zeitpunkt": "2026-09-25T12:00:00+00:00",
        "kanal": {
            "kurs": 4301.80,
            "kurs_zeitpunkt": "2026-09-25T12:00:00+00:00",
            "obere_grenze": 4336.90,
            "untere_grenze": 4162.28,
            "kanalbreite": 174.62,
            "abstand_oben": 35.10,
            "abstand_unten": 139.51,
            "abstand_oben_pct": 0.81,
            "abstand_unten_pct": 3.24,
            "position_im_kanal_pct": 79.9,
        },
    }
    intraday_zukunft = {
        "status": "ok",
        "chartstruktur": chartstruktur,
        "frames": {
            "1h": {"close": 4301.80, "trend": "neutral", "struktur": "bullische Verbesserung", "momentum": "fallend", "ma_lage": "Kurs unter EMA200 und WMA200"},
            "30m": {"trend": "bullisch", "struktur": "höhere Hochs/Höhere Tiefs", "momentum": "fallend", "ma_lage": "Kurs über EMA200 und WMA200"},
            "15m": {"trend": "bullisch", "struktur": "höhere Hochs/Höhere Tiefs", "momentum": "fallend", "ma_lage": "Kurs über EMA200 und WMA200"},
        },
        "bias": "bullisch",
        "score": 3,
        "setup": "bullische Fortsetzung",
        "bull_trigger": 4305.15,
        "bear_trigger": 4244.36,
        "daytrade_resistance": 4315.60,
        "daytrade_support": 4290.19,
    }
    result = mdg.generiere_rueckblick(
        {"realtime": 4301.82, "prev_close": 4290.00}, {}, "Steigend",
        {3: {"widerstandszonen": [], "supportzonen": []}},
        {"naechster_widerstand": 4305.15, "ziel_bullisch": 4335.02, "naechster_support": 4244.36, "ziel_baerisch": 4213.45},
        langfrist_formation="Abwärtskanal",
        mittelfristige_szenarien={"bull": 4391.42, "ziel_bull": 4531.63, "neutral": "4260.99 bis 4391.42", "baer": 4260.99, "ziel_baer": 4006.48},
        intraday_zukunft=intraday_zukunft,
        tages_ma_struktur={"close": 4301.80, "ema20": 4344.61, "ema50": 4337.69, "ema100": 4357.34, "ema200": 4322.75, "wma200": 4425.09, "trendlage": "Kurs unter EMA200 und WMA200", "wma200_richtung": "fallend"},
    )
    prompt = fake_client.models.prompt
    assert result.startswith("Intraday:")
    assert "drei Entwicklungsstufen" in prompt
    assert "Ein Szenario-Trigger ist NICHT automatisch ein Kanal-Ausbruch." in prompt
    assert "4305.15" in prompt
    assert "4336.90" in prompt
    assert "79.9% von unten" in prompt
    assert "nicht als vollständige Trendwende" in prompt
    assert "Eine Marke unterhalb der oberen Kanalgrenze" in prompt
    assert "eine vorgelagerte Hürde" in prompt
    assert "nur tatsächlich über der oberen bzw. unter der unteren Kanalgrenze liegende Strukturen" in prompt

if __name__ == "__main__":
    main()
