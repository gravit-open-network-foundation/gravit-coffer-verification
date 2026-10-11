"""Profile COFFER-SWAP-01: every swap and every reserve, re-computed independently.

For each successful swap in the evidence:
  COF-PRICE   amount out re-computed from the pre-state with the exact integer
              port of the program's math (coffer_verify/math.py) equals the event
  COF-FEE     base fee (rounded up) and protocol share equal the event
  COF-SURGE   sell-off window advanced and surge fee re-computed equal the event
              (NOT_APPLICABLE when the input token has no sell-off cap)
  COF-POST    predicted post-state (virtual, actual, protocol fees owed) equals
              the PoolStateLog the program emitted after the swap
For every transaction that ends with a PoolStateLog:
  COF-RESERVE each pool vault's token balance equals actual balance + protocol
              fees owed for that token (LP reserves and protocol fees are fully
              backed by tokens actually held - no unaccounted drain or surplus)

Pool parameters (weights, fee rates, sell-off curves, active flags) are taken
from the account snapshot and rolled BACK through every parameter change in the
evidence, so each swap is checked against the parameters in effect at that time.
Balances are synchronised from each PoolStateLog the program emits.
"""

import base64
import copy
from typing import Any, Dict, List, Optional

from . import math as M
from .chain import DEC, decode_events, decode_instructions

SELLOFF_KEYS = ("max_selloff_pct", "max_selloff_period_length", "variable_fee_threshold_pct",
                "variable_fee_slope_low_pct", "variable_fee_slope_mid_pct", "variable_fee_slope_high_pct",
                "variable_fee_kink_pct")


def params_from_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    n = acc["token_count"]
    toks = acc["tokens"][:n]
    return {
        "mints": [t["config"]["mint"] for t in toks],
        "weights": [t["config"]["normalized_weight"] for t in toks],
        "swap_fee_rate": acc["swap_fee_rate"],
        "protocol_fee_rate": acc["protocol_fee_rate"],
        "active": [t["config"]["is_active"] for t in toks],
        "selloff": [{k: t["config"][k] for k in SELLOFF_KEYS} for t in toks],
    }


def param_timeline(pool: str, txs: List[Dict[str, Any]], acc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """params_before[i] = parameters in effect before transaction i (oldest first)."""
    cur = params_from_account(acc)
    before: List[Dict[str, Any]] = [None] * len(txs)  # type: ignore
    for i in range(len(txs) - 1, -1, -1):
        tx = txs[i]
        if (tx.get("meta") or {}).get("err") is None:
            for ix in reversed([x for x in decode_instructions(tx) if x["accounts"].get("pool", {}).get("key") == pool]):
                a = ix["args"]
                if ix["name"] == "range_manager_update":
                    for c in a["weight_changes"]:
                        cur["weights"][c["index"]] = c["expected_current"]
                elif ix["name"] == "set_max_selloff":
                    for k in range(len(cur["selloff"])):  # earlier curve unknown in full -> mark unknown
                        cur["selloff"][k] = None
            for name, e in reversed(decode_events(tx)):
                if e.get("pool") != pool:
                    continue
                if name == "SwapFeeRateUpdated":
                    cur["swap_fee_rate"] = e["old_rate"]
                elif name == "ProtocolFeeRateUpdated":
                    cur["protocol_fee_rate"] = e["old_rate"]
                elif name == "TokenActiveSet":
                    cur["active"][e["token_index"]] = e["old_value"]
        before[i] = copy.deepcopy(cur)
    return before


def _vault_balances(tx: Dict[str, Any], pool: str) -> Dict[str, int]:
    out = {}
    for b in (tx.get("meta") or {}).get("postTokenBalances") or []:
        if b.get("owner") == pool:
            out[b["mint"]] = int(b["uiTokenAmount"]["amount"])
    return out


def _cmp(name: str, expected: Dict[str, Any], observed: Dict[str, Any]) -> Dict[str, Any]:
    diff = {k: {"computed": expected[k], "on_chain": observed.get(k)} for k in expected if expected[k] != observed.get(k)}
    return {"id": name, "result": "VERIFIED" if not diff else "INVALID", "detail": diff or None}


def verify_swaps(pool: str, txs: List[Dict[str, Any]], account_b64: str) -> Dict[str, Any]:
    acc, _ = DEC.struct("CubicPool", base64.b64decode(account_b64), 8)
    timeline = param_timeline(pool, txs, acc)
    state: Optional[Dict[str, List[int]]] = None  # vb, actual, fees
    window: List[Optional[Dict[str, int]]] = [None] * acc["token_count"]
    swaps: List[Dict[str, Any]] = []
    reserves = {"VERIFIED": 0, "INVALID": 0, "examples": []}
    for i, tx in enumerate(txs):
        if (tx.get("meta") or {}).get("err") is not None:
            continue
        prm = timeline[i]
        ixs = [x for x in decode_instructions(tx) if x["accounts"].get("pool", {}).get("key") == pool]
        names = {x["name"] for x in ixs}
        if "initialize_cubic_pool" in names:
            window = [{"previous_selloff": 0, "current_selloff": 0, "window_start_timestamp": 0,
                       "selloff_vb_snapshot": 0} for _ in window]
        if names & {"add_liquidity", "remove_liquidity", "set_max_selloff"}:
            window = [None] * len(window)  # rescaling not modelled: resync from the next window event
        events = [(n, e) for n, e in decode_events(tx) if e.get("pool") == pool]
        truncated = "Log truncated" in ((tx.get("meta") or {}).get("logMessages") or [])
        last_log = max((k for k, (n, _) in enumerate(events) if n == "PoolStateLog"), default=-1)
        for ix in ixs:
            if ix["name"] == "range_manager_update" and state is not None:
                for c in ix["args"]["vb_changes"]:
                    state["vb"][c["index"]] = c["new_value"]
        pending: Optional[Dict[str, Any]] = None
        for pos, (name, e) in enumerate(events):
            if name == "MaxSelloffWindowAdvanced":
                window[e["token_index"]] = {"previous_selloff": e["previous_selloff"],
                                            "current_selloff": e["current_selloff"],
                                            "window_start_timestamp": e["window_start_timestamp"],
                                            "selloff_vb_snapshot": e["vb_snapshot"], "_from_event": True}
            elif name == "Swap":
                pending = check_swap(tx, e, prm, state, window)
                swaps.append(pending)
            elif name == "PoolStateLog":
                observed = {"vb": list(e["virtual_balances"]), "actual": list(e["actual_balances"]),
                            "fees": list(e["protocol_fees_owed"])}
                if pending is not None:
                    post = pending.pop("_post", None)
                    if post is None:
                        pending["obligations"].append({"id": "COF-POST", "result": "INSUFFICIENT", "detail": "no pre-state"})
                    else:
                        pending["obligations"].append(_cmp("COF-POST", post, observed))
                    pending["final"] = final(pending["obligations"])
                    pending = None
                state = observed
                if truncated or pos != last_log:
                    continue  # vaults are read after the whole transaction: compare with its final state only
                vaults = _vault_balances(tx, pool)
                for k, mint in enumerate(prm["mints"]):
                    if mint in vaults:
                        held, booked = vaults[mint], observed["actual"][k] + observed["fees"][k]
                        key = "VERIFIED" if held == booked else "INVALID"
                        reserves[key] += 1
                        if key == "INVALID" and len(reserves["examples"]) < 5:
                            reserves["examples"].append({"slot": tx["slot"], "token": k, "vault": held,
                                                         "actual_plus_fees": booked, "difference": held - booked})
        if pending is not None:
            pending.pop("_post", None)
            why = "Solana truncated this transaction's logs" if truncated else "no PoolStateLog"
            pending["obligations"].append({"id": "COF-POST", "result": "INSUFFICIENT", "detail": why})
            pending["final"] = final(pending["obligations"])
        if truncated:
            state = None  # events after the cut are lost: resynchronise from the next PoolStateLog
            window = [None] * len(window)
            reserves["skipped_truncated_logs"] = reserves.get("skipped_truncated_logs", 0) + 1
    return {"swaps": swaps, "reserves": reserves}


def final(obligations: List[Dict[str, Any]]) -> str:
    rs = {o["result"] for o in obligations}
    for r in ("INVALID", "CONFLICTING", "INSUFFICIENT"):
        if r in rs:
            return r
    return "VERIFIED"


def check_swap(tx, e, prm, state, window) -> Dict[str, Any]:
    rec: Dict[str, Any] = {"signature": tx["transaction"]["signatures"][0], "slot": tx["slot"],
                           "event": {k: e[k] for k in ("token_in", "token_out", "amount_in", "amount_out", "fee_amount",
                                                       "protocol_fee_amount", "surge_fee_amount", "timestamp")},
                           "obligations": []}
    obl = rec["obligations"]
    try:
        i, o = prm["mints"].index(e["token_in"]), prm["mints"].index(e["token_out"])
    except ValueError:
        obl.append({"id": "COF-PRICE", "result": "INSUFFICIENT", "detail": "token not in pool"})
        return rec
    amount_in = e["amount_in"]
    fee = M.swap_fee(amount_in, prm["swap_fee_rate"])
    pfee = M.protocol_fee(fee, prm["protocol_fee_rate"])
    obl.append(_cmp("COF-FEE", {"fee_amount": fee, "protocol_fee_amount": pfee}, e))
    if state is None:
        obl.append({"id": "COF-PRICE", "result": "INSUFFICIENT", "detail": "no pre-state"})
        return rec
    vb, act, fees = state["vb"], state["actual"], state["fees"]
    w = prm["weights"]
    cfg = prm["selloff"][i]
    win = None
    surge = 0
    if cfg is None:
        obl.append({"id": "COF-SURGE", "result": "INSUFFICIENT", "detail": "sell-off curve before a change is unknown"})
        surge_known = False
    elif cfg["max_selloff_pct"] == 0:
        obl.append({"id": "COF-SURGE", "result": "NOT_APPLICABLE", "detail": "no sell-off cap on input token"})
        surge_known = True
    elif window[i] is None:
        obl.append({"id": "COF-SURGE", "result": "INSUFFICIENT", "detail": "sell-off window state not yet known"})
        surge_known = False
    else:
        surge_known = True
    in_after = amount_in - fee
    try:
        gross = M.calc_out_given_in(vb[i], w[i], vb[o], w[o], in_after, act[o])
        if cfg and cfg["max_selloff_pct"] and window[i] is not None:
            win = M.advance_selloff(window[i], cfg["max_selloff_pct"], cfg["max_selloff_period_length"],
                                    amount_in, vb[i], e["timestamp"])
            curve = {"threshold": cfg["variable_fee_threshold_pct"], "low": cfg["variable_fee_slope_low_pct"],
                     "mid": cfg["variable_fee_slope_mid_pct"], "high": cfg["variable_fee_slope_high_pct"],
                     "kink": cfg["variable_fee_kink_pct"]}
            surge = M.surge_fee_amount(curve, win, in_after, gross, vb[i], w[i], vb[o], w[o], act[o])
            obl.append(_cmp("COF-SURGE", {"surge_fee_amount": surge}, e))
            window[i] = {k: win[k] for k in ("previous_selloff", "current_selloff", "window_start_timestamp",
                                               "selloff_vb_snapshot")}
    except M.MathError as err:
        obl.append({"id": "COF-PRICE", "result": "INVALID", "detail": f"computation rejects the swap: {err}"})
        return rec
    if not surge_known:
        surge = e["surge_fee_amount"]  # price still checkable: amount_out = gross - surge
    obl.append(_cmp("COF-PRICE", {"amount_out": gross - surge}, e))
    post = {"vb": list(vb), "actual": list(act), "fees": list(fees)}
    net = amount_in - pfee
    post["vb"][i] += net; post["actual"][i] += net; post["fees"][i] += pfee
    post["vb"][o] -= gross; post["actual"][o] -= gross; post["fees"][o] += surge
    rec["_post"] = post
    return rec
