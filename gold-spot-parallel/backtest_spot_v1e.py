name: Backtest Spot-Gold (XAU/USD)

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
        run: pip install -r requirements.txt
      - name: Backtest ausführen
        env:
          APIFREAKS_API_KEY: ${{ secrets.APIFREAKS_API_KEY }}
        run: python backtest_spot_v1e.py
      - name: Ergebnisse als Artefakt hochladen
        uses: actions/upload-artifact@v4
        with:
          name: backtest-spot-ergebnisse
          path: |
            backtest_spot_v1e_trades.csv
            spot_tagesdaten_roh.csv
