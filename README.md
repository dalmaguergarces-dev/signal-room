# Signal Room

A trading room with three bots that read live charts for crypto (Binance) and stocks/ETFs (Yahoo Finance):

- **Atlas**, the chart analyst: long / short / wait calls from trend, RSI, MACD, volume, order flow, order book, VWAP, fair value gaps, big-money zones and a chop (noise) filter.
- **Kelly**, the leverage desk: sizes leverage so a stop-out costs a set % of your account.
- **Redline**, max leverage: shows how close liquidation is and how often it would have been hit.

The chart shows fair value gaps, order blocks, a volume profile and a noise band. You can zoom with pinch, the mouse wheel or buttons.

Not financial advice. The signals are rule-based and often wrong.

## Run locally

    python server.py

Open http://localhost:8765. A phone on the same Wi-Fi can use the address the server prints.

## Host on Render

The repo includes `render.yaml`. In Render choose **New → Blueprint**, then pick this repo. It uses only the Python standard library, so there's nothing to install.
