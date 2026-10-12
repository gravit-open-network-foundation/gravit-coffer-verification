#!/usr/bin/env python3
"""
Experiment C-02: Coffer's public metrics, recomputed from the chain.

  python -m coffer_verify.metrics tvl          # snapshot -> evidence/metrics/tvl-<slot>.json + results/metrics/tvl.json
  python -m coffer_verify.metrics volume START END   # from cached window transactions

Token amounts come from the Solana ledger (authoritative). USD values need a
price source, which is NOT on-chain evidence: Jupiter's public price API
(lite-api.jup.ag/price/v3), recorded raw next to the result with its block id.
"""

import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

from .chain import DEC, PROGRAM_ID, rpc

ROOT = Path(__file__).resolve().parent.parent
PRICE_API = "https://lite-api.jup.ag/price/v3?ids="


def prices(mints):
    out = {}
    mints = sorted(set(mints))
    for i in range(0, len(mints), 50):
        req = urllib.request.Request(PRICE_API + ",".join(mints[i:i + 50]), headers={"User-Agent": "gravit-coffer-verification/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out.update(json.load(r))
    return out


def decimals(mints):
    out = {}
    mints = sorted(set(mints))
    for i in range(0, len(mints), 100):
        res = rpc("getMultipleAccounts", [mints[i:i + 100], {"encoding": "base64"}])
        for m, a in zip(mints[i:i + 100], res["value"]):
            out[m] = base64.b64decode(a["data"][0])[44] if a else None  # SPL mint layout: decimals at byte 44
    return out


def tvl() -> None:
    res = rpc("getProgramAccounts", [PROGRAM_ID, {"encoding": "base64", "withContext": True,
                                                  "filters": [{"dataSize": 1683}]}])
    slot = res["context"]["slot"]
    pools = {}
    for a in res["value"]:
        acc, _ = DEC.struct("CubicPool", base64.b64decode(a["account"]["data"][0]), 8)
        pools[a["pubkey"]] = [{"mint": t["config"]["mint"], "actual": t["dynamics"]["actual_balance"],
                               "fees": t["dynamics"]["protocol_fees_owed"]} for t in acc["tokens"][:acc["token_count"]]]
    mints = [t["mint"] for p in pools.values() for t in p]
    dec, px = decimals(mints), prices(mints)
    ev = {"slot": slot, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pools": pools,
          "decimals": dec, "prices_raw": px}
    d = ROOT / "evidence" / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"tvl-{slot}.json").write_text(json.dumps(ev, indent=1, sort_keys=True) + "\n")
    print(json.dumps(compute_tvl(ev), indent=1)[:2500])


def compute_tvl(ev) -> dict:
    per_pool, unpriced, total_lp, total_all = {}, {}, 0.0, 0.0
    for pool, toks in ev["pools"].items():
        lp = allv = 0.0
        for t in toks:
            p = (ev["prices_raw"].get(t["mint"]) or {}).get("usdPrice")
            dcm = ev["decimals"].get(t["mint"])
            if p is None or dcm is None:
                if t["actual"]:
                    unpriced[t["mint"]] = unpriced.get(t["mint"], 0) + t["actual"]
                continue
            lp += t["actual"] / 10 ** dcm * p
            allv += (t["actual"] + t["fees"]) / 10 ** dcm * p
        per_pool[pool] = round(lp, 2)
        total_lp += lp
        total_all += allv
    out = {"slot": ev["slot"], "fetched_at": ev["fetched_at"], "price_source": PRICE_API.split("?")[0],
           "tvl_usd_lp_reserves": round(total_lp, 2), "tvl_usd_including_protocol_fees": round(total_all, 2),
           "pools": dict(sorted(per_pool.items(), key=lambda x: -x[1])), "unpriced_tokens_raw_amounts": unpriced}
    r = ROOT / "results" / "metrics"
    r.mkdir(parents=True, exist_ok=True)
    (r / "tvl.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return out


STABLE = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}


def volume(start: int, end: int) -> dict:
    """Input-side swap volume of every Coffer pool in [start, end), from Swap events
    in the cached ledger transactions of the window (python -m coffer_verify.window)."""
    from .chain import decode_events

    plan = json.loads((ROOT / ".cache" / f"window-{start}-{end}.json").read_text())
    swaps, missing, seen = [], 0, set()
    for pool, sig in plan:
        if sig in seen:
            continue
        seen.add(sig)
        f = ROOT / ".cache" / "tx" / f"{sig}.json"
        if not f.exists():
            missing += 1
            continue
        tx = json.loads(f.read_text())
        for name, e in decode_events(tx):
            if name == "Swap" and start <= e["timestamp"] < end:
                swaps.append({"pool": e["pool"], "in": e["token_in"], "out": e["token_out"], "amount_in": e["amount_in"],
                              "amount_out": e["amount_out"], "fee": e["fee_amount"], "ts": e["timestamp"]})
    mints = {m for s in swaps for m in (s["in"], s["out"])}
    dec, px = decimals(mints), prices(mints)
    ev_dir = ROOT / "evidence" / "metrics"
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / f"volume-{start}-{end}-prices.json").write_text(json.dumps(
        {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "decimals": dec, "prices_raw": px},
        indent=1, sort_keys=True) + "\n")
    usd = lambda m, a: a / 10 ** dec[m] * px[m]["usdPrice"] if dec.get(m) is not None and (px.get(m) or {}).get("usdPrice") is not None else None
    total = stable_side = fees = 0.0
    unpriced = 0
    per_pool: dict = {}
    for s in swaps:
        # prefer the stablecoin leg (exact USD at swap time); otherwise input token at the snapshot price
        v = (usd(s["in"], s["amount_in"]) if s["in"] in STABLE else
             usd(s["out"], s["amount_out"]) if s["out"] in STABLE else usd(s["in"], s["amount_in"]))
        if v is None:
            unpriced += 1
            continue
        if s["in"] in STABLE or s["out"] in STABLE:
            stable_side += v
        total += v
        fees += usd(s["in"], s["fee"]) or 0.0
        per_pool[s["pool"]] = per_pool.get(s["pool"], 0.0) + v
    out = {"window": {"start": start, "end": end}, "transactions_in_plan": len(seen), "transactions_missing": missing,
           "swaps": len(swaps), "unpriced_swaps": unpriced, "volume_usd": round(total, 2),
           "of_which_priced_by_stablecoin_leg": round(stable_side, 2), "fees_usd": round(fees, 2),
           "price_source_for_other_swaps": PRICE_API.split("?")[0] + " (snapshot at computation time)",
           "pools": {k: round(v, 2) for k, v in sorted(per_pool.items(), key=lambda x: -x[1])}}
    r = ROOT / "results" / "metrics"
    r.mkdir(parents=True, exist_ok=True)
    (r / f"volume-{start}-{end}.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return out


if __name__ == "__main__":
    if sys.argv[1] == "tvl":
        tvl()
    elif sys.argv[1] == "volume":
        print(json.dumps(volume(int(sys.argv[2]), int(sys.argv[3])), indent=1)[:2000])
