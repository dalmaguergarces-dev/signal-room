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
          "DOGE": "DOGEUSDT", "BNB": "BNBUSDT", "AVAX": "AVAXUSDT", "LINK": "LINKUSDT",
          "PAXG": "PAXGUSDT"}  # PAXG = gold-backed token, trades 24/7

# Bar length in seconds, used to tell whether a market is trading right now
TF_SEC = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def asset_class(sym):
    if sym in CRYPTO:
        return "crypto"
    if sym.endswith("=X"):
        return "forex"
    if sym.endswith("=F"):
        return "futures"
    if sym.startswith("^"):
        return "index"
    return "stock"

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
    # Open = the newest bar is fresh. Works for stocks, futures (23h/day) and forex (24h/5d).
    last = (ts[-1] if ts else 0)
    is_open = time.time() - last <= TF_SEC[tf] + 20 * 60
    return {"symbol": sym, "kind": asset_class(sym), "source": "Yahoo Finance",
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


# ------------------------------------------------------------ instant key page
# Followers type their TikTok @ at /get-key and get their own key right away.
# TikTok gives apps no way to check who follows you, so the page asks them to
# follow and you block anyone who doesn't (REVOKED, using their @ without dots).
# Turn the page off with SIGNUP=off.
SIGNUP = os.environ.get("SIGNUP", "on").lower() != "off"
TIKTOK = os.environ.get("TIKTOK_HANDLE", "d0n0j0b").lstrip("@")
HANDLE_RE = re.compile(r"^[a-z0-9_.]{2,24}$")
_signups = []   # recent sign-ups since the server started: (time, handle)
_signup_ips = {}  # ip -> sign-up times (rate limit)


def handle_to_name(handle):
    return handle.replace(".", "_")


# ------------------------------------------------------- phone push alerts
# The server scans ALERT_MARKETS every minute on ALERT_TF. When Atlas opens a
# trade it pushes it through ntfy (free app, no account). Each market has its
# own topic, so people subscribe in the ntfy app to just the markets they want.
# Topic names are derived from OWNER_KEY so outsiders can't guess them.
PUSH = os.environ.get("PUSH", "on").lower() != "off"
NTFY = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
ALERT_TF = os.environ.get("ALERT_TF", "15m")
ALERT_MARKETS = [s.strip().upper() for s in os.environ.get(
    "ALERT_MARKETS", "BTC,ETH,SOL,PAXG,GC=F,SI=F,CL=F,EURUSD=X,SPY,QQQ,TSLA,NVDA,AAPL").split(",") if s.strip()]
PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
NICE = {"PAXG": "Gold 24/7", "GC=F": "Gold", "SI=F": "Silver", "CL=F": "Oil", "BZ=F": "Brent oil",
        "NG=F": "Natural gas", "EURUSD=X": "EUR/USD", "GBPUSD=X": "GBP/USD", "USDJPY=X": "USD/JPY",
        "SPY": "S&P 500", "QQQ": "Nasdaq 100", "^GSPC": "S&P 500", "^IXIC": "Nasdaq"}
CAPS = {"crypto": 10, "futures": 20, "index": 20, "forex": 30, "stock": 4}
_push_state = {"last": {}, "scans": 0, "sent": 0, "error": ""}


def topic_for(sym):
    tag = hmac.new(OWNER_KEY.encode(), b"topic:" + sym.encode(), hashlib.sha256).hexdigest()[:10]
    return f"donojob_{re.sub(r'[^A-Za-z0-9]', '_', sym)}_{tag}"[:64]


def push(topic, title, message, priority=4, tags="chart_with_upwards_trend"):
    body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": [tags]}
    if PUBLIC_URL:
        body["click"] = PUBLIC_URL
    req = urllib.request.Request(NTFY, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **UA}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def scan_once():
    from analysis import analyze  # local module, same logic as the page
    for sym in ALERT_MARKETS:
        try:
            meta = crypto_candles(sym, ALERT_TF) if sym in CRYPTO else yahoo_candles(sym, ALERT_TF)
            a = analyze(meta, depth(sym) if sym in CRYPTO else None)
        except Exception as e:  # one bad market must not stop the rest
            _push_state["error"] = f"{sym}: {type(e).__name__}: {e}"
            continue
        if not a:
            continue
        prev = _push_state["last"].get(sym)
        _push_state["last"][sym] = a["signal"]
        if prev is None or prev == a["signal"] or a["signal"] not in ("LONG", "SHORT"):
            continue
        name = NICE.get(sym, sym)
        stop_pct = abs(a["price"] - a["stop"]) / a["price"]
        lev = max(1.0, min(CAPS.get(meta["kind"], 4), int(0.01 / stop_pct * 10) / 10))
        side = "Long" if a["signal"] == "LONG" else "Short"
        f = lambda x: f"{x:,.2f}" if abs(a['price']) >= 50 else f"{x:,.4f}"
        msg = (f"Entry {f(a['price'])} · Stop {f(a['stop'])} · Target 3:1 {f(a['tp1'])}\n"
               f"Leverage {lev:.1f}x (1% risk) · confidence {a['conf']}% · {ALERT_TF} chart")
        title = f"{side} {name}: trade is set"
        tags = "green_circle" if side == "Long" else "red_circle"
        for t in (topic_for(sym), topic_for("ALL")):
            try:
                push(t, title, msg, tags=tags)
                _push_state["sent"] += 1
            except Exception as e:
                _push_state["error"] = f"push: {type(e).__name__}: {e}"
        print(f"[push] {title} | {msg.splitlines()[0]}", flush=True)


def scanner_loop():
    import threading
    def run():
        while True:
            try:
                scan_once()
                _push_state["scans"] += 1
            except Exception as e:
                _push_state["error"] = f"{type(e).__name__}: {e}"
            time.sleep(60)
    threading.Thread(target=run, daemon=True).start()


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
a{color:var(--accent)}
.alt{margin:14px 0 0;font-size:14px;color:var(--muted)}
.check{display:flex;gap:10px;align-items:flex-start;text-align:left;font-size:13px;color:var(--muted);margin:12px 0 0}
.check input{width:20px;height:20px;flex:none;margin:1px 0 0;accent-color:var(--accent)}
.follow{display:inline-block;margin:0 0 16px;padding:8px 14px;border-radius:999px;border:1px solid var(--accent);text-decoration:none;font-weight:600}
.keybox{font:15px "JetBrains Mono",monospace;background:var(--bg);border:1px dashed var(--accent);border-radius:10px;padding:12px;word-break:break-all;user-select:all}
.small{font-size:13px;color:var(--muted);margin:10px 0 0}
</style></head><body>
<form class="card" method="post" action="/login" id="f">
<img src="/avatar.png" alt="">
<h1>DoNoJob</h1><p>Private signal room. Enter your key.</p>
<input name="key" id="key" type="password" autocomplete="current-password" placeholder="your key" aria-label="Access key" required>
<button type="submit">Enter the room</button>
<div class="err" role="alert">__ERR__</div>
__GETKEY__
</form>
<script>
// Invite links carry the key after the # so it never reaches the server's logs.
function useInvite(){var m=location.hash.match(/^#k-(.+)$/);
if(m){document.getElementById("key").value=decodeURIComponent(m[1]);history.replaceState(null,"","/login");document.getElementById("f").submit()}}
useInvite();window.addEventListener("hashchange",useInvite);
</script></body></html>"""


def page(body_html):
    """Reuse the login page's head and styles around different content."""
    head = LOGIN_HTML.split("<body>")[0]
    return head + "<body><div class=\"card\">" + body_html + "</div></body></html>"


def getkey_form(err=""):
    return page(f"""<form method="post" action="/get-key">
<img src="/avatar.png" alt=""><h1>Get your key</h1>
<p>Free for followers of DoNoJob.</p>
<a class="follow" href="https://www.tiktok.com/@{TIKTOK}" target="_blank" rel="noopener">Follow @{TIKTOK} on TikTok</a>
<input name="handle" autocomplete="username" placeholder="your TikTok @" aria-label="Your TikTok username" required maxlength="25">
<label class="check"><input type="checkbox" name="follows" required> I follow @{TIKTOK}. Keys of people who don't follow get blocked.</label>
<label class="check"><input type="checkbox" name="risk" required> I understand the signals are automated, often wrong and not financial advice. Leverage can wipe out my whole position. I trade at my own risk.</label>
<button type="submit">Get my key</button>
<div class="err" role="alert">{err}</div>
<p class="alt">Already have a key? <a href="/login">Log in</a></p></form>""")


def getkey_done(handle, key):
    k = json.dumps(key)[1:-1]
    return page(f"""<img src="/avatar.png" alt=""><h1>You're in, @{handle}</h1>
<p>This is your personal key. Save it to log in on your other devices.</p>
<div class="keybox" id="k">{k}</div>
<button type="button" id="c">Copy key</button>
<a href="/"><button type="button">Enter the room</button></a>
<p class="small">Don't share it. It's tied to your @, and if it gets passed around it gets blocked.</p>
<script>document.getElementById("c").onclick=function(){{var b=this;navigator.clipboard&&navigator.clipboard.writeText(document.getElementById("k").textContent).then(function(){{b.textContent="Copied"}})}}</script>""")


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
        extra = '<p class="alt">No key yet? <a href="/get-key">Get one free</a></p>' if SIGNUP else ""
        html = LOGIN_HTML.replace("__ERR__", err).replace("__GETKEY__", extra)
        return self.send(code, html, "text/html; charset=utf-8")

    def get_key(self, body):
        if not SIGNUP:
            return self.redirect("/login")
        f = dict(urllib.parse.parse_qsl(body))
        handle = f.get("handle", "").strip().lstrip("@").lower()
        html = lambda e, c: self.send(c, getkey_form(e), "text/html; charset=utf-8")
        if not HANDLE_RE.match(handle):
            return html("That doesn't look like a TikTok @. Use letters, numbers, _ and . only.", 400)
        if not (f.get("follows") and f.get("risk")):
            return html("Tick both boxes to continue.", 400)
        name = handle_to_name(handle)
        if name in REVOKED:
            return html("This @ is blocked. Message @" + TIKTOK + " on TikTok if you think that's a mistake.", 403)
        ip, now = self.ip(), time.time()
        recent = [t for t in _signup_ips.get(ip, []) if now - t < 3600]
        if len(recent) >= 5:
            return html("Too many keys from this connection. Try again in an hour.", 429)
        _signup_ips[ip] = recent + [now]
        key = friend_key(name)
        _signups.append((now, handle))
        del _signups[:-200]
        print(f"[signup] key issued to @{handle}", flush=True)
        self.send(200, getkey_done(handle, key), "text/html; charset=utf-8",
                  {"Set-Cookie": self.cookie_for(key)})

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
        if u.path == "/get-key":
            return self.get_key(body)
        me = self.who()
        if not me:
            return self.send(401, json.dumps({"error": "locked"}))
        if u.path == "/api/push/test":
            if me[0] != "owner":
                return self.send(403, json.dumps({"error": "Only the owner can send a test."}))
            try:
                push(topic_for("ALL"), "DoNoJob test alert",
                     "Phone alerts work. You'll get one like this when a trade is set.", 3, "bell")
                return self.send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self.send(502, json.dumps({"error": f"{type(e).__name__}: {e}"}))
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
            if u.path == "/get-key":
                if not SIGNUP:
                    return self.redirect("/login")
                return self.send(200, getkey_form(), "text/html; charset=utf-8")
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
            if u.path == "/api/push":
                out = {"enabled": PUSH, "tf": ALERT_TF, "server": NTFY,
                       "all": topic_for("ALL"),
                       "markets": {s: topic_for(s) for s in ALERT_MARKETS}}
                if me[0] == "owner":
                    out["status"] = {k: v for k, v in _push_state.items() if k != "last"}
                    out["status"]["signals"] = _push_state["last"]
                return self.send(200, json.dumps(out))
            if u.path == "/api/signups":
                if me[0] != "owner":
                    return self.send(403, json.dumps({"error": "Only the owner can see sign-ups."}))
                return self.send(200, json.dumps({
                    "signup": SIGNUP, "tiktok": TIKTOK,
                    "recent": [{"t": int(t * 1000), "handle": h,
                                "blocked": handle_to_name(h) in REVOKED} for t, h in reversed(_signups)]}))
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
    if PUSH:
        scanner_loop()
        print(f"Phone alerts on: scanning {len(ALERT_MARKETS)} markets every minute ({ALERT_TF})", flush=True)
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()
