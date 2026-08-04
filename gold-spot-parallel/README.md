# Gold Spot Parallel (Test-System)

Eigenständiges, komplett vom bestehenden "Mini Daily Gold"-System getrenntes
Projekt: testet dieselben Positionstrading-Regeln (V1e, siehe
backtest_v1e.py im Hauptprojekt) auf SPOT-Gold (XAU/USD) statt auf dem
Gold-Future (GC=F), um zu prüfen, ob sich die Regeln unverändert übertragen
lassen oder angepasst werden müssen.

## Warum ein neues Projekt?

- Bewusst PARALLEL zum bestehenden System, nicht als Ersatz - das
  bestehende `mini_daily_gold.py` (GC=F, yfinance) bleibt unverändert
- Spot-Daten sind bei kostenlosen APIs immer mit einem Historie-Limit
  verbunden (hier: 10.000 Gratis-Credits bei APIFreaks) - das reicht für
  einen Backtest, aber nicht für einen unbegrenzten Dauerbetrieb wie beim
  bestehenden yfinance/GC=F-System

## Einrichtung

1. Kostenlosen API-Key holen: [apifreaks.com/signup](https://apifreaks.com/signup)
   (kein Kreditkarte nötig, 10.000 Gratis-Credits)
2. Neues Repo anlegen, diese Dateien hineinlegen
3. GitHub Secret `APIFREAKS_API_KEY` mit dem Key hinterlegen
4. Unter "Actions" → "Backtest Spot-Gold (XAU/USD)" → "Run workflow"

## Was der Backtest macht

Lädt die komplette Tageshistorie seit 2019 (in 365-Tage-Häppchen, API-Limit)
und wendet exakt dieselben Regeln an wie `backtest_v1e.py` im Hauptprojekt:
Trendfolge (50-Tage-Regression) + Swing-Tief-Bounce (10 Tage), Stop fest am
Swing-Tief, TP1/TP2 = 2R/3R, Stufenregel, 3-Tage-Cooldown nach Stop.

Ergebnis (Trade-Log + Rohdaten) wird als GitHub-Actions-Artefakt bereitgestellt
- Vergleich mit dem GC=F-Ergebnis (34 Trades, Trefferquote 38,2%, Summe
  +49,77%) zeigt, ob sich Spot und Future hier nennenswert unterscheiden.

## Kosten-Hinweis

Eine komplette 7-Jahres-Historie kostet ca. 7 × 41 = 287 Credits (7 Anfragen
à max. 365 Tage). Bei 10.000 Gratis-Credits sind das ca. 35 vollständige
Backtest-Läufe, bevor die Credits aufgebraucht wären - für die Backtest-Phase
reichlich, für einen täglichen Live-Betrieb (wie beim bestehenden System)
müsste man auf einen kleineren, günstigeren Abruf umstellen (nur die letzten
paar Tage statt der kompletten Historie).
