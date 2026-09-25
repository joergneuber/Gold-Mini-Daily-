# Gold-Mini-Daily

Automatisierte Intraday-, Tages- und Positionsanalyse für **Gold (XAU/USD Spot)** mit technischen Szenarien, Makro-/Wirtschaftskalender, Trade-Alerts, Google-Drive-/E-Mail-Ausgabe und separaten Backtests.

> **Stand dieser Dokumentation:** 25.09.2026  
> Diese README beschreibt den tatsächlich vorliegenden Repository-Stand und nicht eine zukünftige Roadmap.

---

## 1. Was ist Gold-Mini-Daily?

Gold-Mini-Daily ist eine kompakte Research- und Monitoring-Pipeline für Gold. Das Projekt verbindet:

- aktuelle XAU/USD-Spotdaten,
- Intraday-Analyse auf **1h, 30m und 15m**,
- Tages- und 6-Monats-Struktur,
- Pivot-, Support-, Resistance- und Reaktionszonen,
- Trendkanäle und Wendepunkt-Strukturen,
- ein regelbasiertes Positionstrading-Signal,
- einen regelbasierten Range-Ausbruch,
- eine zusätzliche Mean-Reversion-Backtest-Strategie,
- Wirtschafts- und Makrotermine,
- einen neutralen `MACRO_FOCUS` als Kalenderkontext,
- Gemini für die textliche Einordnung,
- separate Trade-Alerts,
- automatische Ablage auf Google Drive,
- E-Mail-Versand,
- GitHub Actions für die wiederkehrende Ausführung.

Das Projekt ist **kein automatisches Handelssystem**. Es erzeugt Analysen, Szenarien und Warnungen. Es führt keine Orders bei einem Broker aus.

---

# 2. Architektur

Der Kernablauf ist:

```text
Twelve Data
   │
   ├── XAU/USD 1h
   ├── XAU/USD 30m
   ├── XAU/USD 15m
   └── XAU/USD 1day
          │
          ▼
┌──────────────────────────────┐
│      mini_daily_gold.py      │
│                              │
│  Intraday                   │
│  Tagesstruktur              │
│  6M-Struktur                │
│  Pivot / Zonen              │
│  Trendkanäle                │
│  Szenarien                  │
│  Positionstrading V1e       │
│  Range-Ausbruch             │
│  Intraday-Zukunftsanalyse   │
└──────────────┬───────────────┘
               │
        ┌──────┴────────┐
        │               │
        ▼               ▼
 economic_events.py    Gemini
        │               │
        │               ▼
        │          Textliche Einordnung
        │
        ▼
 Makro-/Kalenderkontext
               │
               ▼
        HTML / TXT Report
               │
       ┌───────┼─────────┐
       ▼       ▼         ▼
   Google     E-Mail   Trade Alerts
    Drive
```

Die Backtests sind bewusst von der täglichen Report-Erzeugung getrennt:

```text
backtest/
├── backtest_v1e.py

backtest_range_ausbruch.py
backtest_mean_reversion.py
```

Dazu existieren eigene manuell startbare GitHub-Actions.

---

# 3. Datenquelle: Twelve Data

Die aktuelle Produktionsdatenquelle ist **Twelve Data**.

Verwendet wird:

```text
Symbol:    XAU/USD
Asset:     Gold Spot
```

Die Hauptanalyse nutzt:

| Zeitrahmen | Verwendung |
|---|---|
| 1h | Haupt-Intraday-Basis |
| 30m | Setup-/Bestätigungsebene |
| 15m | kurzfristige Trigger-/Bestätigungsebene |
| 1day | Positionstrading und Tagesstruktur |
| 6 Monate | übergeordnete Struktur |

Die Twelve-Data-Abfragen enthalten Retry-/Rate-Limit-Behandlung. Bei HTTP 429 wird gewartet und erneut versucht; auch temporäre Server-/Gateway-Fehler werden behandelt.

Benötigt wird:

```text
TWELVEDATA_API_KEY
```

als GitHub Secret.

---

# 4. Intraday-Analyse

Die Intraday-Analyse arbeitet bewusst mehrstufig.

## 1h

Die 1h-Ebene liefert den übergeordneten Intraday-Kontext:

- Richtung
- Marktstruktur
- Momentum
- EMA-/WMA-Lage
- Chartformation

## 30m

Die 30-Minuten-Ebene dient als mittlere Setup-Ebene.

## 15m

Die 15-Minuten-Ebene dient als kurzfristige Bestätigung bzw. Trigger-Ebene.

Das Projekt erzwingt dabei nicht einfach eine künstliche Einheitsmeinung. Bei widersprüchlichen Signalen wird ausdrücklich ein neutrales Szenario zugelassen.

---

# 5. Intraday-Zukunftsanalyse

Die Funktion `analysiere_intraday_zukunft()` verarbeitet die drei Zeitebenen:

```text
1h   → Richtung
30m  → Setup
15m  → Trigger / Bestätigung
```

Zusätzlich werden Szenario-Marken berücksichtigt:

- bullischer Trigger,
- bärischer Trigger,
- nächste Widerstandsmarke,
- nächste Supportmarke,
- Daytrading-Level.

Ein wichtiger Grundsatz ist:

> Ein Szenario-Trigger ist nicht automatisch ein Ausbruch aus einem übergeordneten Trendkanal.

Diese Unterscheidung wird auch in den Regressionstests abgesichert.

---

# 6. Chartstruktur und Trendkanäle

Das Projekt erkennt unter anderem:

- Aufwärtskanäle,
- Abwärtskanäle,
- Swing-Hochs,
- Swing-Tiefs,
- Wendepunkte,
- Range-Boxen,
- Reaktionszonen.

Dabei werden unterschiedliche Parameter für unterschiedliche Zeitebenen verwendet.

### Tageschart

Der Tageschart besitzt eigene Parameter für:

- Kanalbildung,
- Range-Erkennung,
- Zonen,
- Wendepunkte.

### 6-Monats-Chart

Der langfristige Chart besitzt nochmals größere Fenster und Abstände.

### Intraday

Für Intraday werden kleinere Fenster verwendet.

Diese Trennung ist beabsichtigt: Ein Trendkanal auf Stundenbasis soll nicht mit denselben Parametern berechnet werden wie ein mehrmonatiger Tageskanal.

---

# 7. Pivot-, Support- und Resistance-System

Das Hauptprogramm berechnet:

- klassische Pivot-Level,
- Widerstände,
- Unterstützungen,
- Reaktionszonen,
- kombinierte Zonen,
- nächste relevante Marken.

Die Zonen werden geclustert und zu übersichtlichen Bereichen zusammengeführt.

Sie dienen anschließend als Kontext für:

- Szenarien,
- Charts,
- Positionstrading,
- Range-Ausbruch,
- Gemini-Briefing.

---

# 8. Positionstrading V1e

Das integrierte Positionstrading ist ein **Long-only-Regelsystem**.

Grundregeln:

1. Tagestrend über eine 50-Tage-Regression muss positiv sein.
2. Referenz ist ein rollierendes 10-Tage-Swing-Tief.
3. Der Kurs berührt/unterschreitet das Referenz-Tief.
4. Der Tagesschluss muss anschließend wieder darüber liegen.
5. Entry erfolgt auf Basis dieser bestätigten Bounce-Situation.
6. Stop liegt am Referenz-Tief.
7. TP1 = 2R.
8. TP2 = 3R.
9. Nach TP1 wird der Stop auf Break-even verschoben.
10. Nach TP2 wird der Stop zunächst auf TP1-Niveau gesetzt.
11. Danach wird der Stop am rollierenden 10-Tage-Tief nachgezogen.
12. Nach einem Stop gilt ein Cooldown von 3 Handelstagen.

Dabei wird die historische Simulation für die Kennzahlen aus der vollständigen Historie neu berechnet.

### Wichtige Abgrenzung

Die vollständige historische Simulation und die aktuelle Signal-Anzeige sind getrennt.

Der konfigurierte Signal-Neustart ist:

```text
2026-08-05
```

Damit wird nicht behauptet, dass die historische Strategie erst ab diesem Datum existiert. Das Datum steuert nur, welche simulierten Positionen als aktueller realer Signalzustand angezeigt werden.

---

# 9. Volatilitätsfilter

Für Positionstrading und Range-Ausbruch existiert ein gemeinsamer Volatilitätsfilter.

Grundidee:

```text
ATR kurzfristig
      ÷
ATR langfristig
      ↓
ungewöhnlich hohe Volatilität?
```

Der aktuelle Schalter ist:

```python
VOLATILITAETS_FILTER_AKTIV = False
```

Das bedeutet:

**Der Filter ist im aktuellen Produktionsstand bewusst ausgeschaltet.**

Wenn aktiviert, werden neue Einstiege blockiert, wenn die kurzfristige ATR mehr als das 1,8-Fache der langfristigen ATR beträgt.

Bereits offene Positionen werden davon nicht rückwirkend verändert.

---

# 10. Range-Ausbruch

Das zweite integrierte Signal ist ein **Long-only Range-Ausbruchssystem auf 1h-Basis**.

Regeln:

1. Schlusskurs bricht über das vorherige rollierende 24h-Hoch aus.
2. Der Ausbruch muss durch den Schlusskurs bestätigt sein.
3. Ein reiner Docht genügt nicht.
4. Stop = vorheriges 24h-Tief.
5. TP1 = 2R.
6. TP2 = 3R.
7. Nach TP1 → Break-even.
8. Nach TP2 → Stop zunächst auf TP1.
9. Danach Trailing am 24h-Tief.
10. Nach Stop → 12 Stunden Cooldown.

Der Produktionslauf verwendet nur einen begrenzten historischen Ausschnitt, weil eine vollständige Stundenhistorie bei jedem Report-Lauf unnötig viele Twelve-Data-Anfragen erzeugen würde.

Die vollständige historische Strategieprüfung liegt separat in:

```text
backtest_range_ausbruch.py
```

---

# 11. Range-Ausbruch-Backtest

Der separate Backtest untersucht mehrere Take-Profit-Varianten:

```text
A  → TP1 2R / TP2 3R

C1 → TP1 nächste bestätigte Widerstandszone ab 1R
     TP2 mindestens 3R bzw. nächster Widerstand

C2 → TP1 nächste bestätigte Widerstandszone ab 1,5R

C3 → TP1 nächste bestätigte Widerstandszone ab 2R
```

Gemeinsam sind:

- Long-only,
- 24h-Range,
- bestätigter Close,
- Stop-Regel,
- TP-Stufen,
- 12h Cooldown,
- bestätigte Swing-Highs.

Besonders wichtig:

**Widerstände dürfen nur verwendet werden, wenn sie zum jeweiligen Entry-Zeitpunkt bereits bestätigt waren.**

Damit wird Look-ahead bei der Widerstandsbestimmung vermieden.

Der Backtest erzeugt unter anderem:

```text
range_ausbruch_stundendaten_roh.csv
backtest_range_ausbruch_trades.csv
```

---

# 12. Mean-Reversion-Backtest

`backtest_mean_reversion.py` untersucht bewusst eine andere Marktphase.

V1e und Range-Ausbruch sind Trendfolge-/Ausbruchssysteme. Mean Reversion soll dagegen Seitwärtsphasen untersuchen.

Grundidee:

```text
kein klarer Trend
       +
Nähe zu bestätigtem Support
       +
Bounce
       ↓
Long Mean Reversion
```

Regeln umfassen unter anderem:

- 50-Tage-Trend-/Steigungsfilter,
- 10-Tage-Support,
- bestätigten Bounce,
- Stop am Referenz-Tief,
- TP1 = 1,5R,
- TP2 = 2,5R,
- Cooldown,
- optionalen Volatilitätsfilter.

Der Backtest ist ausdrücklich als **Forschungs-/Prüfstrategie** angelegt. Seine Aufnahme in den produktiven Gold-Report ist nicht automatisch daraus abgeleitet.

---

# 13. Wirtschafts- und Makrokalender

`economic_events.py` stellt eine eigene Kalender-/Makro-Schicht bereit.

Berücksichtigt werden unter anderem:

- ADP
- NFP / Employment Situation
- CPI
- PPI
- JOLTS
- PCE
- GDP
- ISM
- Jobless Claims
- FOMC
- EZB

Datenquellen/Fallbacks umfassen:

- BLS
- BEA
- Federal Reserve
- ECB
- ForexFactory
- lokaler offizieller FOMC-Fallback
- strukturierter Kalender-Fallback.

---

# 14. MACRO_FOCUS

Der Kalender bestimmt einen relevanten Makro-Fokus.

Beispielsweise kann ein für den Tag relevantes Ereignis als:

```text
ADP
CPI
NFP
FOMC
ECB
```

identifiziert werden.

Wichtig:

**MACRO_FOCUS ist kein Trading-Signal.**

Der Kalender soll Kontext liefern und nicht aus einem Wirtschaftstermin automatisch `BUY` oder `SELL` ableiten.

Diese Eigenschaft wird durch Regressionstests abgesichert.

---

# 15. Kalender-Caching

Der Kalender besitzt eigene Cache-Dateien:

```text
economic_events_cache.json
economic_events_forexfactory_cache.json
```

Der Workflow verwendet dafür einen 24-Stunden-orientierten Cache-Mechanismus.

Damit müssen die externen Kalenderquellen nicht bei jedem Zugriff unnötig erneut abgefragt werden.

---

# 16. Gemini

Gemini wird für die textliche Einordnung des Reports verwendet.

Benötigt:

```text
GEMINI_API_KEY
```

Gemini erhält den vom Python-System vorbereiteten Kontext.

Die technische Berechnung der Kurslevel, Zonen, Szenarien und Signale bleibt im Python-Code.

Damit ist die Rolle grundsätzlich:

```text
Python
  ↓
berechnet Fakten / Szenarien
  ↓
Gemini
  ↓
sprachliche Einordnung
```

und nicht:

```text
Gemini
  ↓
erfindet technische Kursdaten
```

---

# 17. Report-Erzeugung

Der Hauptlauf erzeugt:

```text
mini_daily_gold.html
mini_daily_gold.txt
chart.png
chart_tages.png
chart_langfrist.png
trade_alerts.json
```

Der HTML-Report ist die ausführliche Darstellung.

Die TXT-Datei eignet sich für E-Mail bzw. einfache Weiterverarbeitung.

Die Charts decken:

- Intraday,
- Tagesstruktur,
- langfristigen Kontext

ab.

---

# 18. Trade-Alerts

`send_trade_alerts.py` verarbeitet:

```text
trade_alerts.json
```

und führt einen persistenten Status über:

```text
trade_alert_state.json
```

Damit können neue Ereignisse von bereits versendeten Ereignissen unterschieden werden.

Die Alert-Logik ist von der eigentlichen Signalberechnung getrennt.

Ein PREPARE-Alert wird nur erzeugt, wenn ein bestätigter Entry-Trigger noch nicht erreicht wurde, aber der Kurs innerhalb eines definierten Abstands liegt.

---

# 19. Google Drive

`upload_to_drive.py` lädt die erzeugten Ergebnisse automatisch nach Google Drive.

Aktuell werden hochgeladen:

```text
Briefing.html
Briefing.txt
Grafik.png
Grafik-Tages.png
Grafik-6M.png
```

Benötigte Secrets:

```text
GOOGLE_OAUTH_TOKEN_JSON
GOOGLE_DRIVE_FOLDER_ID
```

---

# 20. E-Mail

`send_mail.py` versendet den erzeugten Report per SMTP.

Benötigte Umgebungsvariablen/GitHub Secrets:

```text
SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASSWORD
MAIL_EMPFAENGER
```

Der Versand erfolgt nach der Report-Erzeugung.

---

# 21. GitHub Actions

Der produktive Workflow ist:

```text
.github/workflows/mini_daily_gold.yml
```

Er läuft Montag bis Freitag automatisch zu:

```text
06:45
09:45
13:45
16:45
19:45
```

Zeitzone:

```text
Europe/Berlin
```

Zusätzlich kann der Workflow manuell gestartet werden.

---

# 22. Watchdog / Recovery

Der Hauptworkflow besitzt eine eigene Watchdog-Logik.

Sie prüft nach dem vorgesehenen Zeitfenster, ob der erwartete Schedule-Lauf:

- noch läuft,
- erfolgreich beendet wurde,
- fehlgeschlagen ist,
- oder überhaupt nicht gestartet wurde.

Nach Ablauf des definierten 45-Minuten-Fensters kann ein Recovery-Lauf ausgelöst werden.

Dabei wird nicht blind ein zweiter Lauf gestartet:

- laufende erfolgreiche Jobs werden erkannt,
- ein bereits erfolgreicher Schedule-Lauf verhindert unnötige Wiederholung,
- bei einem fehlenden/fehlgeschlagenen Lauf kann Recovery erfolgen,
- bei einem technischen GitHub-API-Problem gibt es einen zweiten Prüfversuch.

Das ist eine wichtige Robustheitskomponente des automatisierten Betriebs.

---

# 23. Backtest-Workflows

Zusätzlich zum Produktionsworkflow existieren drei manuell startbare Backtest-Workflows:

```text
.github/workflows/backtest_v1e.yml
.github/workflows/backtest_range_ausbruch.yml
.github/workflows/backtest_mean_reversion.yml
```

Sie werden über `workflow_dispatch` gestartet und erzeugen GitHub-Artefakte.

### V1e

```text
backtest/backtest_v1e.py
```

Datenquelle:

```text
yfinance / GC=F
```

Tagesdaten ab 2019.

### Range-Ausbruch

```text
backtest_range_ausbruch.py
```

Datenquelle:

```text
Twelve Data / XAU/USD / 1h
```

### Mean Reversion

```text
backtest_mean_reversion.py
```

Datenquelle:

```text
Twelve Data / XAU/USD / 1day
```

---

# 24. Look-ahead-Schutz

Look-ahead-Vermeidung ist ein wiederkehrendes Prinzip des Projekts.

Beispiele:

- Trendregression verwendet nur bekannte Schlusskurse.
- Swing-Tiefs werden per `shift(1)` erst für den Folgetag verwendet.
- Range-Ausbrüche werden über bestätigte Schlusskurse definiert.
- Swing-Highs für Widerstände müssen durch nachfolgende Kerzen bestätigt sein.
- Die Intraday-Zukunftsanalyse trennt Trigger und Kanal-Ausbruch.

Damit wird versucht zu verhindern, dass historische Berechnungen Informationen verwenden, die zum damaligen Entscheidungszeitpunkt noch nicht bekannt gewesen wären.

---

# 25. Tests

Der aktuelle Repository-Stand enthält zwei explizite Testdateien:

```text
test/test_economic_events.py
test_intraday_zukunft.py
```

### Wirtschaftskalender

Der Regressionstest deckt unter anderem ab:

- Makro-Event-Normalisierung
- False-Positive-Schutz
- ForexFactory-Cache
- FOMC-Fallback
- EZB-Klassifizierung
- unbestätigte Termine am aktuellen Tag
- Kalender-Fallback
- vollständige `lade_termine()`-Pipeline
- MACRO_FOCUS ohne Trading-Signal
- Briefing-Kompatibilität

Aktueller Testlauf:

```text
GOLD_ECONOMIC_EVENTS_TESTS: 10/10 PASS
```

### Intraday-Zukunftsanalyse

Der Smoke-/Regressionstest prüft unter anderem:

- 1h/30m/15m-Struktur,
- Chartkanal,
- Szenario-Trigger,
- Kanalgrenzen,
- Trennung von Trigger und Kanal-Ausbruch,
- Gemini-Prompt-Struktur.

Aktueller Lauf:

```text
SMOKE TEST: OK
```

### Python-Syntax

Der vollständige `compileall`-Lauf über das Repository war im aktuellen Stand ebenfalls erfolgreich.

---

# 26. Repository-Struktur

```text
Gold-Mini-Daily/
│
├── mini_daily_gold.py
├── economic_events.py
├── send_trade_alerts.py
├── send_mail.py
├── upload_to_drive.py
│
├── backtest_range_ausbruch.py
├── backtest_mean_reversion.py
│
├── backtest/
│   ├── backtest_v1e.py
│   └── requirements.txt
│
├── test/
│   └── test_economic_events.py
│
├── test_intraday_zukunft.py
│
├── fomc_termine.json
├── requirements.txt
│
└── .github/
    └── workflows/
        ├── mini_daily_gold.yml
        ├── backtest_v1e.yml
        ├── backtest_range_ausbruch.yml
        ├── backtest_mean_reversion.yml
        └── requirements.txt
```

---

# 27. Benötigte GitHub Secrets

Für den vollständigen automatischen Betrieb werden je nach Funktion benötigt:

### Twelve Data

```text
TWELVEDATA_API_KEY
```

### Gemini

```text
GEMINI_API_KEY
```

### SMTP

```text
SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASSWORD
MAIL_EMPFAENGER
```

### Google Drive

```text
GOOGLE_OAUTH_TOKEN_JSON
GOOGLE_DRIVE_FOLDER_ID
```

Backtests, die Twelve Data verwenden, benötigen ebenfalls:

```text
TWELVEDATA_API_KEY
```

Der V1e-Backtest verwendet dagegen `yfinance` und benötigt dafür keinen Twelve-Data-Key.

---

# 28. Installation lokal

```bash
git clone <REPOSITORY-URL>
cd Gold-Mini-Daily

python -m pip install -r requirements.txt
```

Für den V1e-Backtest:

```bash
python -m pip install -r backtest/requirements.txt
```

---

# 29. Lokaler Hauptlauf

Der Hauptreport kann mit den erforderlichen Umgebungsvariablen lokal gestartet werden:

```bash
python mini_daily_gold.py
```

Danach werden die Report-Dateien im Projektverzeichnis erzeugt.

---

# 30. Backtests lokal starten

### V1e

```bash
python backtest/backtest_v1e.py
```

### Range-Ausbruch

```bash
python backtest_range_ausbruch.py
```

### Mean Reversion

```bash
python backtest_mean_reversion.py
```

Die Backtests sind bewusst eigenständige Forschungsprogramme und verändern nicht automatisch die produktive Signal-Logik.

---

# 31. Aktueller Entwicklungsstand

Das Projekt ist inzwischen mehr als ein einfacher Gold-Tagesbericht.

Es verbindet:

```text
Marktdaten
   +
Intraday-Struktur
   +
Tages-/Langfrist-Struktur
   +
Makrokalender
   +
regelbasierte Signale
   +
Backtesting
   +
Trade Alerts
   +
Gemini
   +
automatisierte Distribution
```

Die besondere Stärke liegt in der Kombination aus **technischer Analyse, Makro-Kontext und separater historischer Strategieprüfung**.

---

# 32. Was besonders gut gelöst ist

## Klare Trennung von Analyse und Alerting

Die Signalberechnung erzeugt Daten; `send_trade_alerts.py` kümmert sich separat um deren Versand.

## Mehrere Zeitebenen

1h, 30m, 15m, Tageschart und 6-Monats-Kontext ergänzen sich, statt dieselbe Analyse mehrfach mit identischen Parametern auszuführen.

## Look-ahead-Bewusstsein

Mehrere zentrale Regeln sind explizit so implementiert, dass nur zum jeweiligen Zeitpunkt bekannte Daten verwendet werden.

## Eigene Backtests

V1e, Range-Ausbruch und Mean Reversion können unabhängig voneinander getestet werden.

## Robuster automatisierter Betrieb

Retries, Rate-Limit-Behandlung, Cache, Watchdog und Recovery reduzieren die Abhängigkeit von einem einzelnen perfekten GitHub-Run.

## Makro-Fokus bleibt neutral

Der Wirtschaftskalender liefert Kontext und Priorisierung, erzeugt aber selbst kein `BUY`/`SELL`-Signal.

---

# 33. Offene Punkte / Grenzen

### 1. Derivatebene

Das Projekt analysiert Gold Spot. Ein Optionsschein oder Knock-out reagiert jedoch nicht 1:1 auf XAU/USD.

Eine echte Derivatebewertung würde zusätzliche Größen benötigen, beispielsweise:

- Delta,
- implizite Volatilität,
- Laufzeit,
- Spread,
- Finanzierung,
- Knock-out-Abstand.

### 2. Realistische Backtest-Ausführung

Für eine noch belastbarere quantitative Bewertung könnten später ergänzt werden:

- Spread,
- Slippage,
- Transaktionskosten,
- Ausführungsmodell,
- Liquiditätsannahmen.

### 3. Out-of-Sample / Walk-Forward

Die vorhandenen Backtests sind historische Regeltests. Ein systematischer Walk-Forward-/Out-of-Sample-Prozess wäre eine sinnvolle spätere Erweiterung.

### 4. Gemeinsames Strategiemodell

Ein Teil der Produktionslogik und die Backtests verwenden bereits sehr ähnliche Regeln. Langfristig wäre eine gemeinsame Strategy-Engine interessant, damit Live- und Backtest-Regeln nicht auseinanderlaufen.

### 5. Daten-Provenance

Eine spätere Erweiterung könnte für jede wichtige Zahl zusätzlich speichern:

```text
Quelle
Beobachtungsdatum
Abrufzeitpunkt
Zeitzone
Datenstatus
```

Damit wäre jede Aussage noch besser reproduzierbar.

---

# 34. Grundprinzip für weitere Änderungen

Bei Änderungen an diesem Projekt sollte gelten:

> **Bestehende Logik nicht unnötig verändern.**

Insbesondere bei:

- Entry-Regeln,
- Exit-Regeln,
- Stop-/TP-Logik,
- Look-ahead-Schutz,
- Makro-Fokus,
- Alert-Status,
- Watchdog,
- Workflow-Zeitplan.

Neue Funktionen sollten möglichst isoliert ergänzt und anschließend gegen die bestehende Funktionalität getestet werden.

---

# 35. Qualitätssicherung vor einem Release

Empfohlener Ablauf:

```text
1. Repository-Version sichern
          ↓
2. Datei-/Bytevergleich
          ↓
3. Zeilenweiser Vergleich
          ↓
4. Struktureller Vergleich
          ↓
5. Semantischer/logischer Vergleich
          ↓
6. Vollständigkeitsprüfung
          ↓
7. Regressionstests
          ↓
8. compileall
          ↓
9. Backtest-Ausführung
          ↓
10. GitHub-Workflow-Prüfung
          ↓
11. Finaler SHA-256-Hash
```

Damit bleibt nachvollziehbar, welche Änderungen tatsächlich vorgenommen wurden und ob bestehende Funktionalität erhalten blieb.

---

## Kurzfassung

**Gold-Mini-Daily** ist eine automatisierte Gold-Research-Pipeline für XAU/USD Spot.

Sie verbindet:

**Twelve Data → Intraday/Tages-/Langfrist-Analyse → Szenarien → Makro-Kalender → regelbasierte Signale → Backtests → Gemini-Kontext → Trade Alerts → Google Drive/E-Mail.**

Das Projekt ist dabei bewusst kein vollautomatischer Trading-Bot. Es erzeugt nachvollziehbare technische und makroökonomische Informationen und kann daraus regelbasierte Setups und Alerts ableiten.
