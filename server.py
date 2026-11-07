"""DoNoJob signal room server.

Serves index.html behind an access key and proxies free market data:
  /api/candles?symbol=BTC&tf=15m   -> normalized OHLCV + taker-buy volume
  /api/depth?symbol=BTC            -> order book (crypto only)

Crypto comes from Binance's public market-data mirror (data-api.binance.vision).
Stocks / ETFs / indices come from Yahoo Finance's chart endpoint.
Standard library only. Run:  python server.py   then open http://localhost:8765

Access keys
  OWNER_KEY  your master key (env var). Locally, if unset, one is generated once
             and saved in the .owner_key file next to this script.
  Friend keys look like "name-1a2b3c4d5e6f7a8b". They are signed with the owner
  key, so nothing is stored. Block a friend by adding their name to REVOKED
  (comma separated env var). Changing OWNER_KEY cancels every friend key at once.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
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


# ---------------------------------------------------------------- access keys
def load_owner_key():
    k = os.environ.get("OWNER_KEY", "").strip()
    if k:
        return k
    path = os.path.join(HERE, ".owner_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    k = "owner-" + secrets.token_urlsafe(18)
    with open(path, "w", encoding="utf-8") as f:
        f.write(k)
    return k


OWNER_KEY = load_owner_key()
REVOKED = {n.strip().lower() for n in os.environ.get("REVOKED", "").split(",") if n.strip()}
NAME_RE = re.compile(r"^[a-z0-9_]{1,24}$")


def sign(name):
    return hmac.new(OWNER_KEY.encode(), b"friend:" + name.encode(), hashlib.sha256).hexdigest()[:16]


def friend_key(name):
    return f"{name}-{sign(name)}"


def check_key(key):
    """Return (role, name) for a valid key, else None."""
    key = (key or "").strip()
    if not key:
        return None
    if hmac.compare_digest(key.encode(), OWNER_KEY.encode()):
        return ("owner", "DoNoJob")
    name, _, sig = key.rpartition("-")
    name = name.lower()
    if NAME_RE.match(name) and name not in REVOKED and hmac.compare_digest(sig.lower().encode(), sign(name).encode()):
        return ("friend", name)
    return None


_fails = {}  # ip -> recent failed login times (rate limit)


def too_many_fails(ip):
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 600]
    return len(_fails[ip]) >= 10


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>DoNoJob</title><meta name="theme-color" content="#151c29">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="DoNoJob">
<link rel="apple-touch-icon" href="/icon.png"><link rel="icon" href="/icon.png">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,800&family=IBM+Plex+Sans:wght@400;600&family=JetBrains+Mono:wght@400&display=swap">
<style>
:root{--bg:#0e131d;--panel:#151c29;--line:#263247;--fg:#dfe6f2;--muted:#8392aa;--accent:#f2b84b;--short:#ef5f6b;color-scheme:dark}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif;display:grid;place-items:center;padding:24px max(16px,env(safe-area-inset-left,0px))}
.card{width:min(380px,100%);background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:28px 24px;text-align:center}
img{width:96px;height:96px;border-radius:50%;border:3px solid var(--accent)}
h1{font:800 32px/1 "Bricolage Grotesque",system-ui,sans-serif;margin:14px 0 4px}
p{color:var(--muted);margin:0 0 20px}
input{width:100%;font:16px "JetBrains Mono",monospace;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
input:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button{width:100%;margin-top:10px;padding:12px;border:0;border-radius:10px;background:var(--accent);color:#1a1405;font-weight:600;font-size:16px;font-family:inherit;cursor:pointer}
.err{color:var(--short);min-height:1.5em;margin:10px 0 0;font-size:14px}
</style></head><body>
<form class="card" method="post" action="/login" id="f">
<img src="/avatar.png" alt="">
<h1>DoNoJob</h1><p>Private signal room. Enter your key.</p>
<input name="key" id="key" type="password" autocomplete="current-password" placeholder="your key" aria-label="Access key" required>
<button type="submit">Enter the room</button>
<div class="err" role="alert">__ERR__</div>
</form>
<script>
// Invite links carry the key after the # so it never reaches the server's logs.
function useInvite(){var m=location.hash.match(/^#k-(.+)$/);
if(m){document.getElementById("key").value=decodeURIComponent(m[1]);history.replaceState(null,"","/login");document.getElementById("f").submit()}}
useInvite();window.addEventListener("hashchange",useInvite);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=None):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, where, cookie=None):
        h = {"Location": where}
        if cookie is not None:
            h["Set-Cookie"] = cookie
        self.send(303, b"", "text/plain", h)

    def ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def cookie_for(self, key, max_age=31536000):
        c = f"sr_key={urllib.parse.quote(key)}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
        return c + ("; Secure" if self.headers.get("X-Forwarded-Proto", "") == "https" else "")

    def who(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sr_key":
                return check_key(urllib.parse.unquote(v))
        return None

    def static(self, name, ctype):
        with open(os.path.join(HERE, name), "rb") as f:
            return self.send(200, f.read(), ctype)

    def login_page(self, err="", code=200):
        return self.send(code, LOGIN_HTML.replace("__ERR__", err), "text/html; charset=utf-8")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        body = self.rfile.read(n).decode("utf-8", "replace")
        if u.path == "/login":
            ip = self.ip()
            if too_many_fails(ip):
                return self.login_page("Too many wrong keys. Try again in 10 minutes.", 429)
            key = dict(urllib.parse.parse_qsl(body)).get("key", "").strip()
            if check_key(key):
                _fails.pop(ip, None)
                return self.redirect("/", self.cookie_for(key))
            _fails.setdefault(ip, []).append(time.time())
            return self.login_page("That key doesn't work. Check it and try again.", 401)
        me = self.who()
        if not me:
            return self.send(401, json.dumps({"error": "locked"}))
        if u.path == "/api/keys":
            if me[0] != "owner":
                return self.send(403, json.dumps({"error": "Only the owner can make keys."}))
            try:
                name = str(json.loads(body or "{}").get("name", "")).strip().lower()
            except ValueError:
                name = ""
            name = re.sub(r"[^a-z0-9_]", "_", name)[:24]
            if not NAME_RE.match(name):
                return self.send(400, json.dumps({"error": "Type a name using letters or numbers."}))
            note = "This name is blocked in REVOKED, so this key won't work." if name in REVOKED else ""
            return self.send(200, json.dumps({"name": name, "key": friend_key(name), "note": note}))
        return self.send(404, json.dumps({"error": "not found"}))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = dict(urllib.parse.parse_qsl(u.query))
        try:
            # ---- open to everyone
            if u.path == "/healthz":
                return self.send(200, b"ok", "text/plain")
            if u.path == "/avatar.png":
                return self.static("avatar.png", "image/png")
            if u.path in ("/icon.png", "/apple-touch-icon.png", "/favicon.ico"):
                return self.static("icon.png", "image/png")
            if u.path == "/login":
                return self.redirect("/") if self.who() else self.login_page()
            if u.path == "/logout":
                return self.redirect("/login", self.cookie_for("", 0))
            # ---- key required from here on
            me = self.who()
            if not me:
                if u.path.startswith("/api/"):
                    return self.send(401, json.dumps({"error": "locked"}))
                return self.redirect("/login")
            if u.path in ("/", "/index.html"):
                return self.static("index.html", "text/html; charset=utf-8")
            if u.path == "/api/me":
                return self.send(200, json.dumps({"role": me[0], "name": me[1]}))
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
    print(f"DoNoJob running at http://localhost:{PORT}  (Ctrl+C to stop)", flush=True)
    ip = lan_ip()
    if host != "127.0.0.1" and ip:
        print(f"On your phone (same Wi-Fi):  http://{ip}:{PORT}", flush=True)
    if not os.environ.get("OWNER_KEY"):
        print("Your owner key is in the .owner_key file next to server.py", flush=True)
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()
