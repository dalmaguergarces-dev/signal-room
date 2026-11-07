"""Follower leaderboard from verified Hyperliquid wallets.

A member links a wallet by signing a DoNoJob message in Phantom (free, no
transaction). The server checks the signature, then reads that wallet's public
trade history from Hyperliquid (which Entropy runs on) and ranks members by
realized profit on the markets DoNoJob covers.

Links live in memory. The member's browser keeps its signed proof and sends it
again on every visit, so the board rebuilds itself after a server restart.
"""
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.request

from eth_hash.auto import keccak
from eth_keys import keys

HL = "https://api.hyperliquid.xyz/info"
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Hyperliquid market names for everything DoNoJob covers (crypto + the "xyz" book).
MARKETS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "AVAX", "LINK", "PAXG",
           "xyz:GOLD", "xyz:SILVER", "xyz:CL", "xyz:BRENTOIL", "xyz:NATGAS", "xyz:COPPER", "xyz:PLATINUM",
           "xyz:SP500", "xyz:XYZ100", "xyz:EUR", "xyz:GBP", "xyz:JPY",
           "xyz:TSLA", "xyz:NVDA", "xyz:AAPL", "xyz:MSFT", "xyz:AMZN", "xyz:META", "xyz:GOOGL",
           "xyz:AMD", "xyz:COIN", "xyz:MSTR", "xyz:NFLX", "xyz:PLTR", "xyz:HOOD", "xyz:INTC", "xyz:ORCL",
           "xyz:MU", "xyz:TSM", "xyz:AVGO", "xyz:ARM", "xyz:GME", "xyz:RIVN", "xyz:BABA", "xyz:CRWD",
           "xyz:NET", "xyz:RDDT", "xyz:IBM", "xyz:DELL", "xyz:QCOM", "xyz:ASML", "xyz:LLY"}

_lock = threading.Lock()
_links = {}    # member name -> {"address": "0x..", "t": linked time}
_stats = {}    # member name -> computed stats
_state = {"updated": 0, "error": ""}


def _secret(owner_key):
    return hmac.new(owner_key.encode(), b"wallet-link", hashlib.sha256).digest()


def challenge(owner_key, name):
    """A message for the member to sign. Its tag proves DoNoJob issued it for this member."""
    ts = int(time.time())
    tag = hmac.new(_secret(owner_key), f"{name}:{ts}".encode(), hashlib.sha256).hexdigest()[:16]
    return (f"DoNoJob wallet link\n\nI own this wallet and want it on the DoNoJob leaderboard.\n"
            f"This costs nothing and sends no transaction.\n\nMember: @{name}\nIssued: {ts}\nCode: {tag}")


def _recover(message, signature):
    sig = bytes.fromhex(signature[2:] if signature.startswith("0x") else signature)
    if len(sig) != 65:
        raise ValueError("bad signature length")
    v = sig[64]
    v = v - 27 if v >= 27 else v
    if v not in (0, 1):
        raise ValueError("bad signature")
    data = message.encode("utf-8")
    digest = keccak(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)
    return keys.Signature(sig[:64] + bytes([v])).recover_public_key_from_msg_hash(digest).to_checksum_address()


def link(owner_key, name, address, message, signature):
    """Verify and store a wallet link. Returns (ok, error)."""
    if not ADDR_RE.match(address or ""):
        return False, "That isn't a wallet address."
    m = re.search(r"Member: @(\S+)\nIssued: (\d+)\nCode: ([0-9a-f]{16})$", message or "")
    if not m or m.group(1) != name:
        return False, "This signature belongs to a different member."
    expect = hmac.new(_secret(owner_key), f"{name}:{m.group(2)}".encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(expect, m.group(3)):
        return False, "That message didn't come from DoNoJob."
    try:
        signer = _recover(message, signature)
    except Exception:
        return False, "The signature couldn't be read. Try linking again."
    if signer.lower() != address.lower():
        return False, "The signature doesn't match that wallet."
    with _lock:
        for other, v in _links.items():
            if other != name and v["address"].lower() == address.lower():
                return False, "That wallet is already linked to another member."
        new = _links.get(name, {}).get("address", "").lower() != address.lower()
        _links[name] = {"address": signer, "t": _links.get(name, {}).get("t") or time.time()}
        if new:
            _stats.pop(name, None)
    if new:
        threading.Thread(target=_refresh_one, args=(name, signer), daemon=True).start()
    return True, ""


def unlink(name):
    with _lock:
        _links.pop(name, None)
        _stats.pop(name, None)


def _fills(address, start_ms):
    body = json.dumps({"type": "userFillsByTime", "user": address, "startTime": start_ms,
                       "aggregateByTime": True}).encode()
    req = urllib.request.Request(HL, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": "DoNoJob leaderboard"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _compute(fills, since_ms):
    pnl = fees = 0.0
    closes = wins = 0
    for f in fills:
        if f.get("coin") not in MARKETS or f.get("time", 0) < since_ms:
            continue
        cp, fee = float(f.get("closedPnl") or 0), float(f.get("fee") or 0)
        pnl += cp
        fees += fee
        if cp != 0:
            closes += 1
            wins += cp > 0
    return {"pnl": round(pnl - fees, 2), "trades": closes, "win_rate": round(100 * wins / closes) if closes else None}


def _refresh_one(name, address):
    now = int(time.time() * 1000)
    try:
        fills = _fills(address, now - 30 * 86400000)
    except Exception as e:
        _state["error"] = f"{name}: {type(e).__name__}"
        return
    with _lock:
        if _links.get(name, {}).get("address") != address:
            return
        _stats[name] = {"d7": _compute(fills, now - 7 * 86400000), "d30": _compute(fills, now - 30 * 86400000),
                        "checked": now}


def refresh_all():
    with _lock:
        todo = [(n, v["address"]) for n, v in _links.items()]
    for n, a in todo:
        _refresh_one(n, a)
        time.sleep(0.3)  # be gentle with the public API
    _state["updated"] = int(time.time() * 1000)


def start(every=300):
    def run():
        while True:
            try:
                refresh_all()
            except Exception as e:
                _state["error"] = f"{type(e).__name__}: {e}"
            time.sleep(every)
    threading.Thread(target=run, daemon=True).start()


def board(period, me_name=None):
    key = "d30" if period == "30d" else "d7"
    with _lock:
        rows = [{"name": n, **_stats[n][key]} for n in _links if n in _stats]
        mine = _links.get(me_name)
    rows.sort(key=lambda r: (-r["pnl"], -r["trades"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["you"] = r["name"] == me_name
    return {"period": period, "rows": rows[:50], "members": len(rows), "updated": _state["updated"],
            "me": {"linked": bool(mine), "address": mine["address"] if mine else "",
                   "row": next((r for r in rows if r["you"]), None)}}
