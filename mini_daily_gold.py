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
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
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

    # yfinance liefert über fast_info oft einen aktuelleren Live-Quote als die
    # 5-Min-Historie (die manchmal mehrere Stunden nachhinkt). Bleibt dieselbe
    # Quelle (GC=F) - kein Wechsel auf Spot/andere Anbieter, also kein
    # Konsistenzproblem zwischen Pivots und Realtime-Wert.
    try:
        live_preis = float(ticker.fast_info["last_price"])
        if live_preis and live_preis > 0:
            realtime = live_preis
    except Exception as exc:
        print(f"fast_info nicht verfügbar, nutze 5-Min-Historie als Realtime-Wert ({exc}).")

    alter_minuten = (pd.Timestamp.now(tz=letzter_zeitpunkt.tz) - letzter_zeitpunkt).total_seconds() / 60
    if alter_minuten > 120:
        print(f"WARNUNG: Letzte Intraday-Kerze ist {alter_minuten:.0f} Minuten alt "
              f"({letzter_zeitpunkt}) - yfinance liefert aktuell verzögerte Daten für GC=F.")

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


def hole_langfrist_daten(monate=36):
    """Tageskurse der letzten `monate` Monate für die Reaktionszonen-Analyse
    (separat von den Intraday-Daten, die für Pivots/Chart genutzt werden).
    yfinance kennt keinen festen period-Code für beliebige Monatszahlen,
    daher wird das Startdatum explizit berechnet."""
    ticker = yf.Ticker(TICKER)
    start = (pd.Timestamp.now() - pd.DateOffset(months=monate)).strftime("%Y-%m-%d")
    daily = ticker.history(start=start, interval="1d")
    if len(daily) < 60:
        return None
    return daily


def analysiere_reaktionszonen(daily, fenster=3, bucket_usd=30, min_treffer=2, top_n=4):
    """Findet lokale Swing-Hochs/-Tiefs (Punkt ist Extremum in einem Fenster von
    +/- `fenster` Handelstagen) und gruppiert sie in Preis-Buckets. Nur Zonen mit
    mindestens `min_treffer` Reaktionen gelten als strukturell relevant - eine
    einzelne Berührung ist noch keine Zone, sondern Zufall."""
    highs = daily["High"].values
    lows = daily["Low"].values
    n = len(daily)

    swing_highs, swing_lows = [], []
    for i in range(fenster, n - fenster):
        fenster_h = highs[i - fenster:i + fenster + 1]
        if highs[i] == fenster_h.max():
            swing_highs.append(highs[i])
        fenster_l = lows[i - fenster:i + fenster + 1]
        if lows[i] == fenster_l.min():
            swing_lows.append(lows[i])

    def clustern(punkte):
        buckets = {}
        for p in punkte:
            key = round(p / bucket_usd) * bucket_usd
            buckets.setdefault(key, []).append(p)
        zonen = [(np.mean(v), len(v)) for v in buckets.values() if len(v) >= min_treffer]
        zonen.sort(key=lambda z: -z[1])
        return zonen[:top_n]

    return {
        "widerstandszonen": clustern(swing_highs),
        "supportzonen": clustern(swing_lows),
    }


def kombiniere_zonen(zonen_je_zeitraum, bucket_usd=30, top_n=6, referenz_preis=None, max_abstand_pct=15):
    """Führt die Zonen aus mehreren Zeitfenstern (z.B. 3/4/36 Monate) zusammen.
    Zonen aus unterschiedlichen Fenstern, die preislich nah beieinander liegen,
    werden zu einer Zone verschmolzen (Trefferzahl = Maximum über die Fenster,
    Zeitfenster vermerkt). Absichtlich MAXIMUM statt Summe: kürzere Fenster (z.B.
    3 Monate) sind meist eine Teilmenge längerer Fenster (z.B. 4 Monate) - dieselben
    Swing-Punkte würden sonst doppelt gezählt, obwohl es nur eine echte Berührung ist.
    Falls referenz_preis übergeben wird, werden Zonen, die mehr als max_abstand_pct
    Prozent vom aktuellen Kurs entfernt liegen, verworfen - sonst können uralte,
    weit entfernte Zonen aus dem 36-Monats-Fenster (z.B. aus einem früheren
    Kurs-Niveau) die Charts unbrauchbar aufblähen, obwohl sie für die aktuelle
    Lage irrelevant sind."""
    def sammle(key):
        buckets = {}
        for monate, zonen in zonen_je_zeitraum.items():
            if not zonen:
                continue
            for preis, treffer in zonen[key]:
                bucket = round(preis / bucket_usd) * bucket_usd
                eintrag = buckets.setdefault(bucket, {"preise": [], "treffer": 0, "fenster": set()})
                eintrag["preise"].append(preis)
                eintrag["treffer"] = max(eintrag["treffer"], treffer)
                eintrag["fenster"].add(monate)
        ergebnis = [
            (sum(e["preise"]) / len(e["preise"]), e["treffer"], sorted(e["fenster"]))
            for e in buckets.values()
        ]
        if referenz_preis:
            ergebnis = [
                z for z in ergebnis
                if abs(z[0] - referenz_preis) / referenz_preis * 100 <= max_abstand_pct
            ]
        ergebnis.sort(key=lambda z: -z[1])
        return ergebnis[:top_n]

    return {
        "widerstandszonen": sammle("widerstandszonen"),
        "supportzonen": sammle("supportzonen"),
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


def hole_saisonalitaet_text():
    """Rein kalenderbasierter Saisonalitäts-Kontext für Gold (Quelle: RealMoneyTrader
    Research, 43 Jahre Historie). Kein API-Aufruf, kein Signal/Qualitäts-Modifikator -
    nur zusätzlicher Kontext für den Rückblick-Text, analog zum Sektor-Analyse-Projekt."""
    heute = datetime.now(ZoneInfo("Europe/Berlin"))
    monat, tag = heute.month, heute.day

    if 5 <= monat <= 8:
        return ("Saisonal befindet sich Gold aktuell in der historisch long-geneigten Phase "
                "Mai bis August (43 Jahre Historie, RealMoneyTrader Research).")
    if (monat == 12 and tag >= 15) or (monat == 1 and tag <= 15):
        return ("Saisonal befindet sich Gold aktuell in der historisch long-geneigten Phase "
                "rund um den Jahreswechsel, Mitte Dezember bis Mitte Januar (43 Jahre Historie, "
                "RealMoneyTrader Research).")
    return None


def generiere_rueckblick(daten, pivots, tendenz, zonen_je_zeitraum):
    """Ruft Gemini auf, um einen kurzen charttechnischen Rückblick-Text zu erzeugen.
    zonen_je_zeitraum: dict {monate: reaktionszonen-dict oder None}, z.B. {3: {...}, 6: {...}, 36: {...}}."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "(Kein GEMINI_API_KEY gesetzt - Rückblick konnte nicht generiert werden.)"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-flash-latest")

    zonen_bloecke = []
    for monate in sorted(zonen_je_zeitraum.keys()):
        zonen = zonen_je_zeitraum[monate]
        if not zonen or (not zonen["widerstandszonen"] and not zonen["supportzonen"]):
            zonen_bloecke.append(f"{monate}-Monats-Fenster: keine Zonen mit mind. 2 Bestätigungen gefunden.")
            continue
        w_zeilen = "; ".join(
            f"ca. {preis:,.0f} USD ({treffer}x)".replace(",", ".")
            for preis, treffer in zonen["widerstandszonen"]
        )
        s_zeilen = "; ".join(
            f"ca. {preis:,.0f} USD ({treffer}x)".replace(",", ".")
            for preis, treffer in zonen["supportzonen"]
        )
        zonen_bloecke.append(
            f"{monate}-Monats-Fenster - Widerstandszonen: {w_zeilen or 'keine'} | "
            f"Supportzonen: {s_zeilen or 'keine'}"
        )
    zonen_block = "\n".join(zonen_bloecke)

    saisonalitaet = hole_saisonalitaet_text()
    saison_block = f"\nSaisonaler Kontext (nur Hintergrundinfo, kein Signal): {saisonalitaet}\n" if saisonalitaet else ""

    prompt = f"""Du bist ein nüchterner charttechnischer Kommentator für Gold (XAU/USD, Future GC=F).
Schreibe einen Rückblick-Absatz (genau 6-7 Sätze, deutsch, sachlich, ohne Anrede,
ohne Kauf-/Verkaufsempfehlung) im Stil eines Intraday-Briefings.

Intraday-Daten (kurzfristig):
- Realtime-Kurs: {daten['realtime']:.2f} USD
- Schlusskurs Vortag: {daten['prev_close']:.2f} USD
- Vortages-Hoch: {daten['prev_high']:.2f} USD
- Vortages-Tief: {daten['prev_low']:.2f} USD
- Intraday-Hoch (aktueller Zeitraum): {daten['intraday_reihe']['Close'].max():.2f} USD
- Intraday-Tief (aktueller Zeitraum): {daten['intraday_reihe']['Close'].min():.2f} USD
- Vorbörsliche Tendenz: {tendenz}
- Intraday-Pivot-Widerstände: {', '.join(f'{v:.0f}' for v in pivots['r'])} USD
- Intraday-Pivot-Unterstützungen: {', '.join(f'{v:.0f}' for v in pivots['s'])} USD
{saison_block}
Strukturelle Reaktionszonen (mehrfach bestätigte Hoch-/Tiefpunkte je Zeitfenster - diese
sind aussagekräftiger für eine Formationsbewertung als die reinen Intraday-Pivots; kürzere
Fenster zeigen eher aktuell relevante Zonen, längere Fenster eher übergeordnete Struktur):
{zonen_block}

Beschreibe zuerst die aktuelle Lage relativ zu den Intraday-Marken. Benenne dabei
EXPLIZIT zwei konkrete Kursszenarien für den Intraday-Horizont:
1. Aufwärtsszenario: welcher Trigger-Kurs einen Ausbruch nach oben auslösen würde und
   welches Kursziel/welche Widerstandsmarke danach als nächstes relevant wird
2. Abwärtsszenario: welcher Trigger-Kurs eine Trendwende/Korrektur nach unten auslösen
   würde und welches Kursziel/welche Unterstützungsmarke danach als nächstes relevant wird
Nenne in beiden Szenarien konkrete Kurswerte aus den Daten oben, keine vagen Formulierungen.

Ordne die Kursbewegung anschließend, gestützt auf die Reaktionszonen der verschiedenen
Zeitfenster (falls vorhanden - bevorzuge dabei das kürzeste Fenster mit brauchbaren
Zonen nahe am aktuellen Kurs), knapp einer gängigen charttechnischen Formation zu (z.B.
aufsteigendes/absteigendes/symmetrisches Dreieck, Seitwärtskanal, Doppel-Top,
Doppel-Boden, Flagge, Keil) und benenne sie explizit im Text. Falls vorhanden, kannst du
den saisonalen Kontext knapp als zusätzliche Einordnung erwähnen - er ersetzt aber nicht
die charttechnische Analyse und ist kein eigenständiges Signal. Falls auch über alle
Zeitfenster hinweg keine seriöse Einschätzung möglich ist, sag das knapp statt zu
spekulieren - keine erfundene Formation nennen, nur um etwas zu benennen.

Bleib trotz der zwei Szenarien und der Formationseinordnung im vorgegebenen Rahmen von
6-7 Sätzen - fasse dich pro Punkt knapp statt jeden Aspekt breit auszuführen.
Keine Übertreibungen, keine Prognosen mit Sicherheit formuliert."""

    try:
        antwort = model.generate_content(prompt)
        return antwort.text.strip()
    except Exception as exc:
        return f"(Rückblick-Generierung fehlgeschlagen: {exc})"


def finde_range_box(intraday_reihe, fenster=4, bucket_usd=6, min_treffer=2):
    """Findet eine horizontale Range: ein Widerstands- und ein Support-Level, die
    beide im Tagesverlauf mehrfach berührt wurden (Swing-Hochs/-Tiefs, analog zur
    Reaktionszonen-Erkennung, aber auf die Intraday-Kerzen angewendet statt auf
    Tagesdaten). Es geht dabei nicht um 'insgesamt wenig Bewegung', sondern um
    wiederholte Berührungen derselben beiden Ebenen - ein echtes Pendeln zwischen
    oben und unten.
    Gibt (start_zeit, end_zeit, tief, hoch) zurück oder None."""
    high = intraday_reihe["High"]
    low = intraday_reihe["Low"]
    n = len(intraday_reihe)

    swing_highs, swing_lows = [], []
    for i in range(fenster, n - fenster):
        fh = high.iloc[i - fenster:i + fenster + 1]
        if high.iloc[i] == fh.max():
            swing_highs.append((high.index[i], high.iloc[i]))
        fl = low.iloc[i - fenster:i + fenster + 1]
        if low.iloc[i] == fl.min():
            swing_lows.append((low.index[i], low.iloc[i]))

    def groesstes_cluster(punkte):
        buckets = {}
        for zeit, preis in punkte:
            key = round(preis / bucket_usd) * bucket_usd
            buckets.setdefault(key, []).append((zeit, preis))
        if not buckets:
            return None
        bestes = max(buckets.values(), key=len)
        if len(bestes) < min_treffer:
            return None
        zeiten = [z for z, _ in bestes]
        preise = [p for _, p in bestes]
        return min(zeiten), max(zeiten), sum(preise) / len(preise)

    r_cluster = groesstes_cluster(swing_highs)
    s_cluster = groesstes_cluster(swing_lows)
    if not r_cluster or not s_cluster:
        return None

    start = min(r_cluster[0], s_cluster[0])
    # Bis zum aktuellsten Kurspunkt verlängern, nicht nur bis zur letzten Berührung -
    # die Range gilt als weiterhin gültig, solange der Kurs sie nicht verlassen hat.
    ende = intraday_reihe.index[-1]
    return start, ende, s_cluster[2], r_cluster[2]


def finde_range_boxen(preisreihe, fenster=5, bucket_usd=30, min_treffer=2, segmente=3):
    """Wie finde_range_box, aber für längere Zeiträume (z.B. 6 Monate) gedacht, in
    denen es mehrere zeitlich getrennte Ranges auf unterschiedlichen Kursniveaus
    geben kann (z.B. bei einem übergeordneten Trend, der durch mehrere Konsolidierungs-
    Phasen unterbrochen wird). Teilt den Zeitraum in `segmente` gleich große,
    chronologische Abschnitte und sucht in jedem Abschnitt separat nach einer Range.
    Gibt eine Liste von (start_zeit, end_zeit, tief, hoch) zurück, maximal
    `segmente` Einträge."""
    n = len(preisreihe)
    grenzen = np.linspace(0, n, segmente + 1).astype(int)
    boxen = []
    for i in range(segmente):
        teil = preisreihe.iloc[grenzen[i]:grenzen[i + 1]]
        if len(teil) < 2 * fenster + min_treffer:
            continue
        box = finde_range_box(teil, fenster=fenster, bucket_usd=bucket_usd, min_treffer=min_treffer)
        if box:
            boxen.append(box)
    return boxen


def finde_intraday_umkehrzonen(intraday_reihe, fenster=3, bucket_usd=5, min_treffer=2, top_n=3):
    """Analog zu analysiere_reaktionszonen (die für Tagesdaten schon existiert), aber
    auf Intraday-Kerzen angewendet: findet Swing-Hochs/-Tiefs und gruppiert sie zu
    Umkehrzonen mit mehreren Berührungen. Anders als finde_range_box werden hier
    NICHT zwei Level zu einer Box gepaart, sondern jede für sich mehrfach bestätigte
    Zone einzeln zurückgegeben - näher an dem, was man beim manuellen Einzeichnen
    mehrerer Widerstands-/Support-Linien im Chart machen würde."""
    high = intraday_reihe["High"]
    low = intraday_reihe["Low"]
    n = len(intraday_reihe)

    swing_highs, swing_lows = [], []
    for i in range(fenster, n - fenster):
        fh = high.iloc[i - fenster:i + fenster + 1]
        if high.iloc[i] == fh.max():
            swing_highs.append(high.iloc[i])
        fl = low.iloc[i - fenster:i + fenster + 1]
        if low.iloc[i] == fl.min():
            swing_lows.append(low.iloc[i])

    def clustern(punkte):
        buckets = {}
        for p in punkte:
            key = round(p / bucket_usd) * bucket_usd
            buckets.setdefault(key, []).append(p)
        zonen = [(sum(v) / len(v), len(v)) for v in buckets.values() if len(v) >= min_treffer]
        zonen.sort(key=lambda z: -z[1])
        return zonen[:top_n]

    return {"widerstandszonen": clustern(swing_highs), "supportzonen": clustern(swing_lows)}


def baue_chart(intraday_reihe, pivots, strukturzonen=None, pfad="chart.png"):
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

    # Umkehrzonen vorab berechnen (wird weiter unten auch fürs Zeichnen genutzt), damit
    # die Range-Box nur gezeigt wird, wenn sie sich mit einer Umkehrzone deckt - sonst
    # zeigen beide fast dieselbe Information doppelt und übereinander im Bild.
    umkehrzonen = finde_intraday_umkehrzonen(intraday_reihe, top_n=2)
    alle_umkehr_preise = (
        [p for p, _ in umkehrzonen["widerstandszonen"]]
        + [p for p, _ in umkehrzonen["supportzonen"]]
    )

    # Range-Box: Widerstand + Support, die beide mehrfach berührt wurden (Swing-Hochs/
    # -Tiefs in 20-Min-Fenstern, min. 2 Berührungen je Seite) - anders als die reine
    # Spannen-Prüfung erkennt das auch Tage mit echtem Pendeln zwischen zwei Levels.
    range_box = finde_range_box(intraday_reihe, fenster=4, bucket_usd=6, min_treffer=2)
    box_bereich = None
    if range_box:
        start_zeit, end_zeit, tief, hoch = range_box
        ueberschneidet_sich = any(tief <= p <= hoch for p in alle_umkehr_preise)
        if ueberschneidet_sich:
            box_bereich = (tief, hoch)
            x_start = mdates.date2num(start_zeit)
            x_end = mdates.date2num(end_zeit)
            ax.add_patch(Rectangle(
                (x_start, tief), x_end - x_start, hoch - tief,
                linewidth=1.5, edgecolor="#e8e0c8", facecolor="none", alpha=0.85, zorder=4,
            ))
            ax.text(start_zeit, hoch, "Range  ", color="#e8e0c8", fontsize=8.5,
                     style="italic", va="bottom", ha="right")

    # Basis-Range: Kursbereich + Puffer
    puffer = (preise.max() - preise.min()) * 0.15
    y_unten = preise.min() - puffer
    y_oben = preise.max() + puffer

    # Nur die EINE wirklich nächstgelegene Marke pro Richtung zieht die Achse (egal ob
    # Pivot oder übergeordnetes Struktur-Level) - vorher zogen beide unabhängig
    # voneinander, wodurch eine weit entfernte Struktur-Zone versehentlich auch alle
    # dazwischenliegenden Pivot-Level mit ins Bild zog und die Skala unnötig aufblähte.
    r_kandidaten = [("pivot", r) for r in pivots["r"] if r > y_oben]
    s_kandidaten = [("pivot", s) for s in pivots["s"] if s < y_unten]
    if strukturzonen:
        r_kandidaten += [("struktur", p) for p, *_ in strukturzonen["widerstandszonen"] if p > y_oben]
        s_kandidaten += [("struktur", p) for p, *_ in strukturzonen["supportzonen"] if p < y_unten]

    naechster_r_typ = naechster_r = None
    if r_kandidaten:
        naechster_r_typ, naechster_r = min(r_kandidaten, key=lambda kv: kv[1])
        y_oben = max(y_oben, naechster_r * 1.002)
    naechster_s_typ = naechster_s = None
    if s_kandidaten:
        naechster_s_typ, naechster_s = max(s_kandidaten, key=lambda kv: kv[1])
        y_unten = min(y_unten, naechster_s * 0.998)

    if naechster_r_typ == "pivot":
        ax.axhline(naechster_r, color="#b5654f", linewidth=1.1, linestyle="--", alpha=0.85)
        ax.text(intraday_reihe.index[-1], naechster_r, f" Widerstand {naechster_r:,.0f}", color="#e8887a",
                 fontsize=9.5, fontweight="bold", va="center", ha="left")
    elif naechster_r_typ == "struktur":
        ax.axhline(naechster_r, color="#b5654f", linewidth=1.3, linestyle=":", alpha=0.6)
        ax.text(intraday_reihe.index[-1], naechster_r, f" Struktur-Widerstand {naechster_r:,.0f}",
                 color="#e8887a", fontsize=8.5, style="italic", va="center", ha="left")

    if naechster_s_typ == "pivot":
        ax.axhline(naechster_s, color="#7fae6f", linewidth=1.1, linestyle="--", alpha=0.85)
        ax.text(intraday_reihe.index[-1], naechster_s, f" Support {naechster_s:,.0f}", color="#9fcf8f",
                 fontsize=9.5, fontweight="bold", va="center", ha="left")
    elif naechster_s_typ == "struktur":
        ax.axhline(naechster_s, color="#7fae6f", linewidth=1.3, linestyle=":", alpha=0.6)
        ax.text(intraday_reihe.index[-1], naechster_s, f" Struktur-Support {naechster_s:,.0f}",
                 color="#9fcf8f", fontsize=8.5, style="italic", va="center", ha="left")

    # Pivot- und Struktur-Level, die zufällig auch noch in die (dadurch minimal
    # erweiterte) Achse passen, zusätzlich einzeichnen - aber nichts zieht die
    # Achse weiter auf als die eine oben ermittelte nächste Marke je Richtung.
    for r in pivots["r"]:
        if r != naechster_r and y_unten <= r <= y_oben:
            ax.axhline(r, color="#b5654f", linewidth=1.1, linestyle="--", alpha=0.85)
            ax.text(intraday_reihe.index[-1], r, f" Widerstand {r:,.0f}", color="#e8887a",
                     fontsize=9.5, fontweight="bold", va="center", ha="left")
    for s in pivots["s"]:
        if s != naechster_s and y_unten <= s <= y_oben:
            ax.axhline(s, color="#7fae6f", linewidth=1.1, linestyle="--", alpha=0.85)
            ax.text(intraday_reihe.index[-1], s, f" Support {s:,.0f}", color="#9fcf8f",
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

    # Umkehrzonen zeichnen: mehrfach berührte Swing-Hochs/-Tiefs, jede einzeln als Linie -
    # nur innerhalb des bereits feststehenden Achsenbereichs, damit sie die Skala nicht
    # erneut aufblähen. Eigene Farbe (Blau) statt Creme, unterscheidbar von der Range-Box.
    # Zonen INNERHALB einer bereits gezeichneten Range-Box werden übersprungen - die
    # Box deckt diesen Preisbereich schon ab, eine zusätzliche Linie wäre redundant
    # und sorgt nur für überlappende Beschriftungen.
    def in_box(p):
        return box_bereich is not None and box_bereich[0] <= p <= box_bereich[1]

    for preis, treffer in umkehrzonen["widerstandszonen"]:
        if y_unten <= preis <= y_oben and not in_box(preis):
            ax.axhline(preis, color="#6fa8dc", linewidth=1.0, linestyle="-", alpha=0.6)
            ax.text(intraday_reihe.index[-1], preis, f"  Umkehrzone {preis:,.0f} ({treffer}x)".replace(",", "."),
                     color="#6fa8dc", fontsize=7.5, va="bottom", ha="right")
    for preis, treffer in umkehrzonen["supportzonen"]:
        if y_unten <= preis <= y_oben and not in_box(preis):
            ax.axhline(preis, color="#6fa8dc", linewidth=1.0, linestyle="-", alpha=0.6)
            ax.text(intraday_reihe.index[-1], preis, f"  Umkehrzone {preis:,.0f} ({treffer}x)".replace(",", "."),
                     color="#6fa8dc", fontsize=7.5, va="bottom", ha="right")

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
    ax.set_ylabel("USD", color="#a89d87", fontsize=10)

    fig.tight_layout()
    fig.savefig(pfad, facecolor=fig.get_facecolor())
    plt.close(fig)
    return pfad


def baue_langfrist_chart(daily, zonen, pfad="chart_langfrist.png"):
    """4-Monats-Tageschart mit den bereits berechneten Reaktionszonen als Linien -
    macht sichtbar, wo die im Rückblick-Text genannten strukturellen Zonen herkommen."""
    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    fig.patch.set_facecolor("#14110d")
    ax.set_facecolor("#14110d")

    schluss = daily["Close"]
    ax.plot(daily.index, schluss, color="#e8b95c", linewidth=1.3)

    # Trendlinie über den gesamten dargestellten Zeitraum (anders als beim Intraday-Chart,
    # wo nur die jüngere Hälfte genutzt wird - hier soll die übergeordnete 4-Monats-Bewegung
    # abgebildet werden, nicht nur ein kurzer Ausschnitt)
    x_num = mdates.date2num(schluss.index)
    steigung, achsenabschnitt = np.polyfit(x_num, schluss.values, 1)
    trend_werte = steigung * x_num + achsenabschnitt
    trend_farbe = "#5cb85c" if steigung > 0 else "#d9534f"
    trend_label = "Aufwärtstrend" if steigung > 0 else "Abwärtstrend"
    ax.plot(schluss.index, trend_werte, color=trend_farbe, linewidth=1.8,
             linestyle="-", alpha=0.9, zorder=5)
    ax.text(schluss.index[0], trend_werte[0], f"{trend_label}  ", color=trend_farbe,
             fontsize=10, fontweight="bold", va="bottom", ha="left")

    # Range-Boxen: gleiche Berührungs-basierte Erkennung wie im Intraday-Chart, aber
    # in 3 zeitliche Abschnitte segmentiert - über 4 Monate kann es mehrere getrennte
    # Ranges auf unterschiedlichen Kursniveaus geben, nicht nur eine einzige.
    range_boxen = finde_range_boxen(daily, fenster=5, bucket_usd=30, min_treffer=2, segmente=3)
    for start_zeit, end_zeit, tief, hoch in range_boxen:
        x_start = mdates.date2num(start_zeit)
        x_end = mdates.date2num(end_zeit)
        ax.add_patch(Rectangle(
            (x_start, tief), x_end - x_start, hoch - tief,
            linewidth=1.5, edgecolor="#e8e0c8", facecolor="none", alpha=0.85, zorder=4,
        ))
        ax.text(end_zeit, hoch, " Range", color="#e8e0c8", fontsize=8.5,
                 style="italic", va="bottom", ha="left")

    if zonen:
        for preis, treffer, fenster in zonen["widerstandszonen"]:
            fenster_txt = "/".join(f"{m}M" for m in fenster)
            ax.axhline(preis, color="#b5654f", linewidth=1.0, linestyle="--", alpha=0.8)
            ax.text(daily.index[-1], preis, f" Widerstand {preis:,.0f} ({treffer}x, {fenster_txt})".replace(",", "."),
                     color="#e8887a", fontsize=9, fontweight="bold", va="center", ha="left")
        for preis, treffer, fenster in zonen["supportzonen"]:
            fenster_txt = "/".join(f"{m}M" for m in fenster)
            ax.axhline(preis, color="#7fae6f", linewidth=1.0, linestyle="--", alpha=0.8)
            ax.text(daily.index[-1], preis, f" Support {preis:,.0f} ({treffer}x, {fenster_txt})".replace(",", "."),
                     color="#9fcf8f", fontsize=9, fontweight="bold", va="center", ha="left")

    ax.margins(x=0.10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.tick_params(colors="#a89d87", labelsize=10)
    for spine in ax.spines.values():
        spine.set_color("#3a3226")
    ax.grid(axis="y", color="#2a251c", linewidth=0.6, alpha=0.8)
    ax.set_title("Gold (GC=F) - 4 Monate, strukturelle Reaktionszonen", color="#ece6d9",
                 fontsize=13, loc="left")
    ax.set_ylabel("USD", color="#a89d87", fontsize=10)

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

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Tageschart (Intraday)</h3>
    <img src="cid:chart" style="max-width:100%;border:1px solid #3a3226;">

    <h3 style="color:#a89d87;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Struktureller Chart (4 Monate)</h3>
    <img src="cid:chart_lang" style="max-width:100%;border:1px solid #3a3226;">

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

    zonen_je_zeitraum = {}
    daily_lang = None
    for monate in (3, 4, 36):
        langfrist = hole_langfrist_daten(monate=monate)
        if monate == 4:
            daily_lang = langfrist
        zonen_je_zeitraum[monate] = analysiere_reaktionszonen(langfrist) if langfrist is not None else None
        if zonen_je_zeitraum[monate]:
            print(f"Widerstandszonen ({monate}M): {zonen_je_zeitraum[monate]['widerstandszonen']}")
            print(f"Supportzonen ({monate}M): {zonen_je_zeitraum[monate]['supportzonen']}")
        else:
            print(f"Keine ausreichenden Daten für {monate}-Monats-Zonen.")

    rueckblick_text = generiere_rueckblick(daten, pivots, tendenz_label, zonen_je_zeitraum)
    # Zwei getrennte Toleranzen: der Intraday-Chart soll nur wirklich naheliegende
    # Struktur-Level zeigen (enger Zeithorizont), der 4-Monats-Chart darf großzügiger sein.
    kombinierte_zonen_intraday = kombiniere_zonen(zonen_je_zeitraum, referenz_preis=daten["realtime"], max_abstand_pct=5)
    # Für den 4-Monats-Chart bewusst OHNE Preisnähe-Filter, aber nur aus den 3-/4-Monats-
    # Fenstern (nicht 36M) - deren Zonen stammen aus Daten, die ohnehin im sichtbaren
    # 4-Monats-Preisbereich liegen, können die Achse also nicht aufblähen. Das 36-Monats-
    # Fenster bleibt außen vor, weil es auch Zonen aus einem ganz anderen (viel tieferen)
    # historischen Kursniveau liefern kann.
    kombinierte_zonen_lang = kombiniere_zonen(
        {k: v for k, v in zonen_je_zeitraum.items() if k in (3, 4)}
    )
    chart_pfad = baue_chart(daten["intraday_reihe"], pivots, strukturzonen=kombinierte_zonen_intraday)
    chart_lang_pfad = None
    if daily_lang is not None:
        chart_lang_pfad = baue_langfrist_chart(daily_lang, kombinierte_zonen_lang)
    html = baue_html(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text, chart_pfad)
    text = baue_text(daten, pivots, tendenz_label, tendenz_pct, rueckblick_text)

    with open("mini_daily_gold.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("mini_daily_gold.txt", "w", encoding="utf-8") as f:
        f.write(text)

    print(f"Realtime: {daten['realtime']:.2f} USD | Tendenz: {tendenz_label} ({tendenz_pct:+.2f}%)")
    print(f"Widerstände: {pivots['r']}")
    print(f"Unterstützungen: {pivots['s']}")
    print("Report geschrieben: mini_daily_gold.html, mini_daily_gold.txt, chart.png, chart_langfrist.png")


if __name__ == "__main__":
    sys.exit(main() or 0)
