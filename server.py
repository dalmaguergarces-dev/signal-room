"""Signal Room local server.

Serves index.html and proxies free market data so the page can read it:
  /api/candles?symbol=BTC&tf=15m   -> normalized OHLCV + taker-buy volume
  /api/depth?symbol=BTC            -> order book (crypto only)

Crypto comes from Binance's public market-data mirror (data-api.binance.vision).
Stocks / ETFs / indices come from Yahoo Finance's chart endpoint.
Standard library only. Run:  python server.py   then open http://localhost:8765
"""
import json
import os
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (SignalRoom local app)"}

CRYPTO = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT",
          "DOGE": "DOGEUSDT", "BNB": "BNBUSDT", "AVAX": "AVAXUSDT", "LINK": "LINKUSDT"}

# Yahoo: interval and how much history to request for it
YAHOO_TF = {"5m": ("5m", "5d"), "15m": ("15m", "1mo"), "1h": ("60m", "3mo"),
            "4h": ("60m", "6mo"), "1d": ("1d", "2y")}

_cache = {}


def fetch_json(url, ttl):
    now = time.time()
    hit = _cache.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    _cache[url] = (now, data)
    return data


def crypto_candles(sym, tf):
    pair = CRYPTO[sym]
    url = (f"https://data-api.binance.vision/api/v3/klines?symbol={pair}"
           f"&interval={tf}&limit=500")
    raw = fetch_json(url, 2)
    candles = [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                "c": float(k[4]), "v": float(k[5]), "tb": float(k[9])} for k in raw]
    return {"symbol": sym, "kind": "crypto", "source": "Binance", "open": True,
            "flow": "taker", "candles": candles}


def yahoo_candles(sym, tf):
    interval, rng = YAHOO_TF[tf]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(sym)}?interval={interval}&range={rng}&includePrePost=false")
    data = fetch_json(url, 15)
    res = data["chart"]["result"][0]
    meta = res.get("meta", {})
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    candles = []
    for i, t in enumerate(ts):
        o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i], q["close"][i],
                         q["volume"][i])
        if None in (o, h, l, c):
            continue
        candles.append({"t": t * 1000, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
    if tf == "4h":  # Yahoo has no 4h bars: merge 60m bars in groups of 4
        merged = []
        for i in range(0, len(candles), 4):
            g = candles[i:i + 4]
            merged.append({"t": g[0]["t"], "o": g[0]["o"], "h": max(x["h"] for x in g),
                           "l": min(x["l"] for x in g), "c": g[-1]["c"],
                           "v": sum(x["v"] for x in g)})
        candles = merged
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now = time.time()
    is_open = bool(period) and period.get("start", 0) <= now <= period.get("end", 0)
    return {"symbol": sym, "kind": "stock", "source": "Yahoo Finance",
            "name": meta.get("shortName") or meta.get("longName") or sym,
            "open": is_open, "flow": "estimated", "candles": candles[-500:]}


def depth(sym):
    pair = CRYPTO[sym]
    raw = fetch_json(f"https://data-api.binance.vision/api/v3/depth?symbol={pair}&limit=100", 2)
    return {"bids": [[float(p), float(q)] for p, q in raw["bids"]],
            "asks": [[float(p), float(q)] for p, q in raw["asks"]]}


def make_icon(size=180):
    """Home-screen icon: dark tile, green 'live' dot, amber signal bars."""
    import struct
    import zlib
    bg, amber, green = (21, 28, 41), (242, 184, 75), (47, 196, 154)
    bars = [(66, 40), (98, 68), (130, 96)]  # left x, height; all end at y=146
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            c = bg
            if (x - 46) ** 2 + (y - 46) ** 2 <= 14 ** 2:
                c = green
            for bx, bh in bars:
                if bx <= x < bx + 22 and 146 - bh <= y < 146:
                    c = amber
            row += bytes(c)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


ICON = make_icon()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path in ("/icon.png", "/apple-touch-icon.png"):
                return self.send(200, ICON, "image/png")
            if u.path == "/api/candles":
                sym = qs.get("symbol", "BTC").upper()
                tf = qs.get("tf", "15m")
                if tf not in YAHOO_TF:
                    return self.send(400, json.dumps({"error": "bad timeframe"}))
                out = crypto_candles(sym, tf) if sym in CRYPTO else yahoo_candles(sym, tf)
                return self.send(200, json.dumps(out))
            if u.path == "/api/depth":
                sym = qs.get("symbol", "BTC").upper()
                if sym not in CRYPTO:
                    return self.send(200, json.dumps({"bids": [], "asks": []}))
                return self.send(200, json.dumps(depth(sym)))
            return self.send(404, json.dumps({"error": "not found"}))
        except Exception as e:  # report data errors to the page instead of crashing
            return self.send(502, json.dumps({"error": f"{type(e).__name__}: {e}"}))


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    # HOST=0.0.0.0 (default) lets a phone on the same Wi-Fi open it.
    # Set HOST=127.0.0.1 to keep it to this PC only.
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Signal Room running at http://localhost:{PORT}  (Ctrl+C to stop)")
    ip = lan_ip()
    if host != "127.0.0.1" and ip:
        print(f"On your phone (same Wi-Fi):  http://{ip}:{PORT}")
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()
