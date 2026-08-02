"""
Mini Daily: Gold
-----------------
Holt aktuelle Goldkurse, berechnet Intraday-Pivot-Level (Widerstände/Unterstützungen),
lässt einen kurzen Rückblick-Text von Gemini generieren, baut einen Tageschart
und erzeugt daraus einen HTML-Report. Der Report wird anschließend (in main.py-
Aufrufern bzw. separaten Schritten) nach Google Drive hochgeladen und per Mail verschickt.

Datenquelle: yfinance (GC=F, Gold-Future). Kein API-Key nötig.
"""

import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import yfinance as yf
import google.generativeai as genai

TICKER = "GC=F"  # Gold-Future. Yahoo/yfinance bietet keinen zuverlässig abrufbaren
                  # Spot-Ticker (XAUUSD=X / XAU=X liefern 404) - GC=F liegt fast immer
                  # nur wenige USD neben dem echten Spot-Kurs.
SEITWAERTS_SCHWELLE_PROZENT = 0.15  # +/- Band um Vortagesschluss für "Seitwärts"


def hole_kursdaten():
    """Liefert Realtime-Kurs, Vortages-OHLC und eine Intraday-Kursreihe für den Chart."""
    ticker = yf.Ticker(TICKER)

    # Intraday-Reihe (5-Min, letzte 2 Tage) - liefert auch den aktuellsten Kurs
    intraday = ticker.history(period="2d", interval="5m")
    if intraday.empty:
        raise RuntimeError("Keine Intraday-Daten von yfinance erhalten (GC=F).")

    realtime = float(intraday["Close"].iloc[-1])
    letzter_zeitpunkt = intraday.index[-1]

    # Tages-Reihe für Vortages-OHLC (Pivot-Basis)
    daily = ticker.history(period="5d", interval="1d")
    if len(daily) < 2:
        raise RuntimeError("Nicht genug Tagesdaten für Pivot-Berechnung.")

    vortag = daily.iloc[-2]
    prev_high = float(vortag["High"])
    prev_low = float(vortag["Low"])
    prev_close = float(vortag["Close"])

    return {
        "realtime": realtime,
        "letzter_zeitpunkt": letzter_zeitpunkt,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "prev_close": prev_close,
        "intraday_reihe": intraday,
    }


def klassische_pivots(high, low, close):
    p = (high + low + close) / 3
    r1 = 2 * p - low
    s1 = 2 * p - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    r3 = high + 2 * (p - low)
    s3 = low - 2 * (high - p)
    return {"p": p, "r": [r1, r2, r3], "s": [s1, s2, s3]}


def bestimme_tendenz(realtime, prev_close):
    pct = (realtime - prev_close) / prev_close * 100
    if pct > SEITWAERTS_SCHWELLE_PROZENT:
        return "Steigend", pct
    elif pct < -SEITWAERTS_SCHWELLE_PROZENT:
        return "Fallend", pct
    return "Seitwärts", pct


def generiere_rueckblick(daten, pivots, tendenz):
    """Ruft Gemini auf, um einen kurzen charttechnischen Rückblick-Text zu erzeugen."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "(Kein GEMINI_API_KEY gesetzt - Rückblick konnte nicht generiert werden.)"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-flash-latest")

    prompt = f"""Du bist ein nüchterner charttechnischer Kommentator für Gold (XAU/USD, Future GC=F).
Schreibe einen kurzen Rückblick-Absatz (4-6 Sätze, deutsch, sachlich, ohne Anrede,
ohne Kauf-/Verkaufsempfehlung) im Stil eines Intraday-Briefings.

Daten:
- Realtime-Kurs: {daten['realtime']:.2f} USD
- Schlusskurs Vortag: {daten['prev_close']:.2f} USD
- Vortages-Hoch: {daten['prev_high']:.2f} USD
- Vortages-Tief: {daten['prev_low']:.2f} USD
- Intraday-Hoch (aktueller Zeitraum): {daten['intraday_reihe']['Close'].max():.2f} USD
- Intraday-Tief (aktueller Zeitraum): {daten['intraday_reihe']['Close'].min():.2f} USD
- Vorbörsliche Tendenz: {tendenz}
- Widerstände: {', '.join(f'{v:.0f}' for v in pivots['r'])} USD
- Unterstützungen: {', '.join(f'{v:.0f}' for v in pivots['s'])} USD

Beschreibe zuerst die aktuelle Lage relativ zu diesen Marken (Nähe zu einem Widerstand/
einer Unterstützung, mögliche Trigger-Kurse für einen Ausbruch nach oben oder eine
Trendwende nach unten). Nenne konkrete Kurswerte aus den Daten oben.

Ordne die Kursbewegung außerdem, soweit anhand der Werte erkennbar, einer gängigen
charttechnischen Formation zu (z.B. aufsteigendes/absteigendes/symmetrisches Dreieck,
Seitwärtskanal, Doppel-Top, Doppel-Boden, Flagge, Keil) und benenne sie explizit im Text.
Falls die Datenlage für eine seriöse Formations-Einschätzung nicht ausreicht, sag das
knapp statt zu spekulieren - keine erfundene Formation nennen, nur um etwas zu benennen.

Keine Übertreibungen, keine Prognosen mit Sicherheit formuliert."""

    try:
        antwort = model.generate_content(prompt)
        return antwort.text.strip()
    except Exception as exc:
        return f"(Rückblick-Generierung fehlgeschlagen: {exc})"


def baue_chart(intraday_reihe, pivots, pfad="chart.png"):
    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    fig.patch.set_facecolor("#14110d")
    ax.set_facecolor("#14110d")

    preise = intraday_reihe["Close"]
    ax.plot(intraday_reihe.index, preise, color="#e8b95c", linewidth=1.6)

    # Trendlinie: einfache lineare Regression über die letzte Hälfte der Kursreihe
    # (aktuellerer Trend statt über den gesamten 2-Tage-Zeitraum gemittelt)
    trend_ausschnitt = preise.iloc[len(preise) // 2:]
    x_num = mdates.date2num(trend_ausschnitt.index)
    steigung, achsenabschnitt = np.polyfit(x_num, trend_ausschnitt.values, 1)
    trend_werte = steigung * x_num + achsenabschnitt
    trend_farbe = "#5cb85c" if steigung > 0 else "#d9534f"
    trend_label = "Aufwärtstrend" if steigung > 0 else "Abwärtstrend"
    ax.plot(trend_ausschnitt.index, trend_werte, color=trend_farbe, linewidth=1.8,
             linestyle="-", alpha=0.9, zorder=5)
    ax.text(trend_ausschnitt.index[-1], trend_werte[-1], f"  {trend_label}", color=trend_farbe,
             fontsize=10, fontweight="bold", va="bottom" if steigung > 0 else "top", ha="left")

    # Basis-Range: Kursbereich + Puffer
    puffer = (preise.max() - preise.min()) * 0.15
    y_unten = preise.min() - puffer
    y_oben = preise.max() + puffer

    # Die jeweils nächstgelegene Widerstands-/Unterstützungslinie IMMER mit einbeziehen,
    # auch wenn sie knapp außerhalb des reinen Kursbereichs liegt - sonst fehlt oft
    # jede Orientierung nach oben oder unten.
    r_oberhalb = [r for r in pivots["r"] if r > y_oben]
    if r_oberhalb:
        y_oben = max(y_oben, min(r_oberhalb) * 1.002)
    s_unterhalb = [s for s in pivots["s"] if s < y_unten]
    if s_unterhalb:
        y_unten = min(y_unten, max(s_unterhalb) * 0.998)

    for r in pivots["r"]:
        if y_unten <= r <= y_oben:
            ax.axhline(r, color="#b5654f", linewidth=1.1, linestyle="--", alpha=0.85)
            ax.text(intraday_reihe.index[-1], r, f" {r:,.0f}", color="#e8887a",
                     fontsize=9.5, fontweight="bold", va="center", ha="left")
    for s in pivots["s"]:
        if y_unten <= s <= y_oben:
            ax.axhline(s, color="#7fae6f", linewidth=1.1, linestyle="--", alpha=0.85)
            ax.text(intraday_reihe.index[-1], s, f" {s:,.0f}", color="#9fcf8f",
                     fontsize=9.5, fontweight="bold", va="center", ha="left")

    # Tatsächliches Intraday-Hoch/-Tief zusätzlich als schlichte Referenzlinien -
    # ergänzt die rechnerischen Pivot-Level um die real erreichten Extrempunkte.
    intraday_hoch = preise.max()
    intraday_tief = preise.min()
    ax.axhline(intraday_hoch, color="#c9c2b0", linewidth=0.9, linestyle=":", alpha=0.7)
    ax.text(intraday_reihe.index[0], intraday_hoch, "Tageshoch  ", color="#c9c2b0",
             fontsize=8.5, va="bottom", ha="left")
    ax.axhline(intraday_tief, color="#c9c2b0", linewidth=0.9, linestyle=":", alpha=0.7)
    ax.text(intraday_reihe.index[0], intraday_tief, "Tagestief  ", color="#c9c2b0",
             fontsize=8.5, va="top", ha="left")

    ax.set_ylim(y_unten, y_oben)
    ax.margins(x=0.08)  # Platz rechts für die Level-Beschriftungen

    # Feineres Gitter: Hauptlinien + gedämpfte Zwischenlinien für bessere Ablesbarkeit
    spanne = y_oben - y_unten
    schrittweite = 5 if spanne < 80 else (10 if spanne < 160 else 20)
    ax.yaxis.set_major_locator(plt.MultipleLocator(schrittweite))
    ax.grid(axis="y", color="#2a251c", linewidth=0.6, alpha=0.8)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    ax.tick_params(colors="#a89d87", labelsize=10)
    for spine in ax.spines.values():
        spine.set_color("#3a3226")
    ax.set_title("Gold (GC=F) - Intraday", color="#ece6d9", fontsize=13, loc="left")

    fig.tight_layout()
    fig.savefig(pfad, facecolor=fig.get_facecolor())
    plt.close(fig)
    return pfad


def baue_text(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text):
    heute = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%A, %d. %B %Y")
    zeit = daten["letzter_zeitpunkt"].strftime("%d.%m. %H:%M")

    def liste(werte):
        return " / ".join(f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") for v in werte)

    def fmt(n):
        return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    text = f"""MINI DAILY: GOLD
{heute} - Stand {zeit}

VORBOERSLICHE TENDENZ
{tendenz_label} ({tendenz_pct:+.2f}%)

WIDERSTAENDE (INTRADAY)
{liste(pivots['r'])} USD

UNTERSTUETZUNGEN (INTRADAY)
{liste(pivots['s'])} USD

REALTIME INDIKATION
{fmt(daten['realtime'])} USD

SCHLUSSKURS (VORTAG)
{fmt(daten['prev_close'])} USD

RUECKBLICK
{rueckblick_text}

---
Kein Kauf-/Verkaufssignal - reine charttechnische Orientierung - Datenquelle: yfinance
"""
    return text


def baue_html(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, chart_dateiname):
    heute = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%A, %d. %B %Y")
    zeit = daten["letzter_zeitpunkt"].strftime("%d.%m. %H:%M")

    def level_liste(werte, farbe):
        return "".join(
            f'<span style="display:inline-block;background:#241f16;border-left:3px solid {farbe};'
            f'padding:6px 12px;margin:4px 6px 4px 0;border-radius:2px;font-family:monospace;">'
            f'{v:,.2f}</span>'.replace(",", "X").replace(".", ",").replace("X", ".")
            for v in werte
        )

    html = f"""
    <html><body style="background:#14110d;color:#ece6d9;font-family:monospace;padding:20px;">
    <h1 style="color:#e8b95c;font-family:serif;">Mini Daily: Gold</h1>
    <p style="color:#a89d87;">{heute} · Stand {zeit}</p>
    <hr style="border-color:#3a3226;">

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Vorbörsliche Tendenz</h3>
    <p style="font-size:20px;font-family:serif;">{tendenz_label} ({tendenz_pct:+.2f}%)</p>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Widerstände (Intraday)</h3>
    <div>{level_liste(pivots['r'], '#b5654f')}</div>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Unterstützungen (Intraday)</h3>
    <div>{level_liste(pivots['s'], '#7fae6f')}</div>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Realtime Indikation</h3>
    <p style="font-size:28px;font-family:serif;color:#e8b95c;">{daten['realtime']:,.2f} USD</p>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Schlusskurs (Vortag)</h3>
    <p style="font-size:20px;font-family:serif;">{daten['prev_close']:,.2f} USD</p>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Rückblick</h3>
    <p style="line-height:1.6;">{rueckblick_text}</p>

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Tageschart</h3>
    <img src="cid:chart" style="max-width:100%;border:1px solid #3a3226;">

    <p style="color:#a89d87;font-size:10px;margin-top:24px;">
    Kein Kauf-/Verkaufssignal · reine charttechnische Orientierung · Datenquelle: yfinance
    </p>
    </body></html>
    """
    return html


def main():
    daten = hole_kursdaten()
    pivots = klassische_pivots(daten["prev_high"], daten["prev_low"], daten["prev_close"])
    tendenz_label, tendenz_pct = bestimme_tendenz(daten["realtime"], daten["prev_close"])
    rueckblick_text = generiere_rueckblick(daten, pivots, tendenz_label)
    chart_pfad = baue_chart(daten["intraday_reihe"], pivots)
    html = baue_html(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, chart_pfad)
    text = baue_text(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text)

    with open("mini_daily_gold.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("mini_daily_gold.txt", "w", encoding="utf-8") as f:
        f.write(text)

    print(f"Realtime: {daten['realtime']:.2f} USD | Tendenz: {tendenz_label} ({tendenz_pct:+.2f}%)")
    print(f"Widerstände: {pivots['r']}")
    print(f"Unterstützungen: {pivots['s']}")
    print("Report geschrieben: mini_daily_gold.html, mini_daily_gold.txt, chart.png")


if __name__ == "__main__":
    sys.exit(main() or 0)
