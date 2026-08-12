name: Backtest Range-Ausbruch (XAU/USD, 1h)

on:
  workflow_dispatch: {}

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Abhängigkeiten installieren
        run: pip install requests pandas numpy
      - name: Backtest ausführen
        env:
          TWELVEDATA_API_KEY: ${{ secrets.TWELVEDATA_API_KEY }}
        run: python backtest_range_ausbruch.py
      - name: Ergebnisse als Artefakt hochladen
        uses: actions/upload-artifact@v4
        with:
          name: backtest-range-ausbruch-ergebnisse
          path: |
            backtest_range_ausbruch_C1C2C3_vergleich.csv
            backtest_range_ausbruch_C1C2C3_trades.csv
            backtest_range_ausbruch_risikolimit_vergleich.csv
            range_ausbruch_stundendaten_roh.csv
