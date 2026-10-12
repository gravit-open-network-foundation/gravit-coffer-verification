"""Experiment C-01: independent verification of Coffer range-manager transitions.

For one pool, replays the full ledger history in order, reconstructs the state
that matters to the range manager (policy in effect, manager, weights, virtual
and actual balances, time of the last update) and checks every successful
`range_manager_update` against the policy that was in effect at that moment.

Obligations (profile COFFER-RM-01, program cubic_pool 0.5.1):
  COF-AUTH  signer is the configured range manager, and the manager is enabled
  COF-VB    per changed virtual balance: |new - current| <= cap * current (cap 0: no change),
            applied where actual reserves are positive
  COF-WT    resulting weights in [100, 9900] bps, sum 10000; per change
            |new - current| <= cap * current
  COF-LEV   every live token (actual > 0): min <= virtual / actual <= max (bps; 0 disables a side)
  COF-INT   time since the previous successful update >= min interval
  COF-STATE the pre-state the manager declared (expected_current) equals the
            independently reconstructed state

Results: VERIFIED, INVALID, NOT_APPLICABLE, INSUFFICIENT (data missing),
CONFLICTING (declared and reconstructed state disagree).
"""

import hashlib
import json
from typing import Any, Dict, List, Optional

from .chain import IDL_PATH, PROGRAM_ID, decode_events, decode_instructions

PCT = 10_000
MIN_W, MAX_W, W_SCALE = 100, 9_900, 10_000


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


IDL_SHA256 = hashlib.sha256(IDL_PATH.read_bytes()).hexdigest()


def within_cap(cur: int, new: int, cap: int) -> bool:
    if cap == 0:
        return new == cur
    return abs(new - cur) * PCT <= cap * cur


ZERO_KEY = "11111111111111111111111111111111"  # the program's "no range manager"


class PoolState:
    def __init__(self) -> None:
        self.manager: Optional[str] = ZERO_KEY
        self.enabled = False
        self.cfg = {"max_vb_change_pct": 0, "max_weight_change_pct": 0, "min_update_interval_secs": 0,
                    "max_leverage_bps": 0, "min_leverage_bps": 0}
        self.weights: Optional[List[int]] = None
        self.vb: Optional[List[int]] = None
        self.actual: Optional[List[int]] = None
        self.last_update = 0


def check_update(st: PoolState, ix: Dict[str, Any], ts: int) -> List[Dict[str, Any]]:
    a, auth = ix["args"], ix["accounts"].get("authority", {})
    cfg = st.cfg
    res: List[Dict[str, Any]] = []

    if st.manager is None:
        res.append({"id": "COF-AUTH", "result": "INSUFFICIENT", "detail": "manager before the evidence window unknown"})
    else:
        ok = bool(auth.get("signer")) and auth.get("key") == st.manager and st.enabled
        res.append({"id": "COF-AUTH", "result": "VERIFIED" if ok else "INVALID",
                    "detail": {"signer": auth.get("key"), "manager": st.manager, "enabled": st.enabled}})
    if any(v is None for v in cfg.values()):
        res.append({"id": "COF-CONFIG", "result": "INSUFFICIENT", "detail": {"policy_in_effect": cfg}})
        return res

    # declared pre-state vs reconstructed pre-state
    conflicts = []
    for c in a["vb_changes"]:
        if st.vb is not None and st.vb[c["index"]] != c["expected_current"]:
            conflicts.append({"vb_index": c["index"], "declared": c["expected_current"], "reconstructed": st.vb[c["index"]]})
    for c in a["weight_changes"]:
        if st.weights is not None and st.weights[c["index"]] != c["expected_current"]:
            conflicts.append({"weight_index": c["index"], "declared": c["expected_current"], "reconstructed": st.weights[c["index"]]})
    if st.vb is None or (a["weight_changes"] and st.weights is None):
        res.append({"id": "COF-STATE", "result": "INSUFFICIENT", "detail": "no reconstructed pre-state"})
    else:
        res.append({"id": "COF-STATE", "result": "CONFLICTING" if conflicts else "VERIFIED", "detail": conflicts})

    # virtual balance band
    if not a["vb_changes"]:
        res.append({"id": "COF-VB", "result": "NOT_APPLICABLE", "detail": "no virtual balance change"})
    elif st.actual is None:
        res.append({"id": "COF-VB", "result": "INSUFFICIENT", "detail": "actual balances unknown"})
    else:
        rows = []
        for c in a["vb_changes"]:
            applies = st.actual[c["index"]] > 0
            rows.append({"index": c["index"], "current": c["expected_current"], "new": c["new_value"],
                         "change_bps": round(abs(c["new_value"] - c["expected_current"]) * PCT / c["expected_current"], 2)
                         if c["expected_current"] else None,
                         "cap_bps": cfg["max_vb_change_pct"], "applies": applies,
                         "ok": (not applies) or within_cap(c["expected_current"], c["new_value"], cfg["max_vb_change_pct"])})
        res.append({"id": "COF-VB", "result": "VERIFIED" if all(r["ok"] for r in rows) else "INVALID", "detail": rows})

    # weights
    if not a["weight_changes"]:
        res.append({"id": "COF-WT", "result": "NOT_APPLICABLE", "detail": "no weight change"})
    elif st.weights is None:
        res.append({"id": "COF-WT", "result": "INSUFFICIENT", "detail": "weights unknown"})
    else:
        new_w = list(st.weights)
        rows = []
        for c in a["weight_changes"]:
            new_w[c["index"]] = c["new_value"]
            rows.append({"index": c["index"], "current": c["expected_current"], "new": c["new_value"],
                         "cap_bps": cfg["max_weight_change_pct"],
                         "ok": within_cap(c["expected_current"], c["new_value"], cfg["max_weight_change_pct"])})
        live = [w for w in new_w if w]
        vector_ok = sum(new_w) == W_SCALE and all(MIN_W <= w <= MAX_W for w in live)
        res.append({"id": "COF-WT", "result": "VERIFIED" if vector_ok and all(r["ok"] for r in rows) else "INVALID",
                    "detail": {"changes": rows, "new_weights": new_w, "sum": sum(new_w), "vector_ok": vector_ok}})

    # leverage envelope after the update
    lo, hi = cfg["min_leverage_bps"], cfg["max_leverage_bps"]
    if lo == 0 and hi == 0:
        res.append({"id": "COF-LEV", "result": "NOT_APPLICABLE", "detail": "no leverage band configured"})
    elif st.actual is None or st.vb is None:
        res.append({"id": "COF-LEV", "result": "INSUFFICIENT", "detail": "balances unknown"})
    else:
        vb_after = list(st.vb)
        for c in a["vb_changes"]:
            vb_after[c["index"]] = c["new_value"]
        rows = []
        for i, (v, act) in enumerate(zip(vb_after, st.actual)):
            if act <= 0:
                continue
            ok = (hi == 0 or v * PCT <= hi * act) and (lo == 0 or v * PCT >= lo * act)
            rows.append({"index": i, "leverage_x": round(v / act, 4), "ok": ok})
        res.append({"id": "COF-LEV", "result": "VERIFIED" if all(r["ok"] for r in rows) else "INVALID",
                    "detail": {"min_bps": lo, "max_bps": hi, "tokens": rows}})

    # minimum interval
    gap = ts - st.last_update if st.last_update else None
    if cfg["min_update_interval_secs"] == 0 or gap is None:
        res.append({"id": "COF-INT", "result": "NOT_APPLICABLE" if cfg["min_update_interval_secs"] == 0 else "VERIFIED",
                    "detail": {"seconds_since_previous": gap, "min": cfg["min_update_interval_secs"]}})
    else:
        res.append({"id": "COF-INT", "result": "VERIFIED" if gap >= cfg["min_update_interval_secs"] else "INVALID",
                    "detail": {"seconds_since_previous": gap, "min": cfg["min_update_interval_secs"]}})
    return res


def final_result(obligations: List[Dict[str, Any]]) -> str:
    rs = {o["result"] for o in obligations}
    for r in ("INVALID", "CONFLICTING", "INSUFFICIENT"):
        if r in rs:
            return r
    return "VERIFIED"


def initial_from_snapshot(pool: str, txs: List[Dict[str, Any]], acc: Dict[str, Any]) -> PoolState:
    """For evidence that starts after the pool's creation: roll the range-manager
    settings back from the account snapshot through every change in the evidence.
    What cannot be recovered stays None (-> INSUFFICIENT)."""
    st = PoolState()
    st.manager, st.enabled = acc["range_manager"], acc["range_manager_enabled"]
    st.cfg = {"max_vb_change_pct": acc["range_manager_max_vb_change_pct"],
              "max_weight_change_pct": acc["range_manager_max_weight_change_pct"],
              "min_update_interval_secs": acc["range_manager_min_update_interval_secs"],
              "max_leverage_bps": acc["range_manager_max_leverage_bps"],
              "min_leverage_bps": acc["range_manager_min_leverage_bps"]}
    n = acc["token_count"]
    st.weights = [t["config"]["normalized_weight"] for t in acc["tokens"][:n]]
    for tx in reversed(txs):
        if (tx.get("meta") or {}).get("err") is not None:
            continue
        for ix in reversed([x for x in decode_instructions(tx) if x["accounts"].get("pool", {}).get("key") == pool]):
            if ix["name"] == "range_manager_update":
                for c in ix["args"]["weight_changes"]:
                    st.weights[c["index"]] = c["expected_current"]
        for name, e in reversed(decode_events(tx)):
            if e.get("pool") != pool:
                continue
            if name == "RangeManagerSet":
                st.manager, st.enabled = e["old_manager"], e["old_enabled"]
            elif name == "RangeManagerConfigSet":
                st.cfg = {"max_vb_change_pct": e["old_max_vb_change_pct"],
                          "max_weight_change_pct": e["old_max_weight_change_pct"],
                          "min_update_interval_secs": e["old_min_update_interval_secs"],
                          "max_leverage_bps": e["old_max_leverage_bps"],
                          "min_leverage_bps": None}  # not carried by the event
    return st


def replay(pool: str, txs: List[Dict[str, Any]], acc: Optional[Dict[str, Any]] = None,
           from_genesis: bool = True) -> Dict[str, Any]:
    """`txs`: the pool's transactions, oldest first (raw getTransaction JSON)."""
    st = PoolState() if from_genesis or acc is None else initial_from_snapshot(pool, txs, acc)
    records: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    undecodable = 0
    for tx in txs:
        sig = tx["transaction"]["signatures"][0]
        failed = (tx.get("meta") or {}).get("err") is not None
        ixs = [i for i in decode_instructions(tx) if i["accounts"].get("pool", {}).get("key") == pool]
        if failed:
            if any(i["name"] == "range_manager_update" for i in ixs):
                rejected.append({"signature": sig, "slot": tx["slot"], "error": tx["meta"]["err"]})
            continue
        events = decode_events(tx)
        undecodable += sum(1 for n, _ in events if n == "UNDECODABLE")
        for ix in ixs:
            a = ix["args"]
            if "UNDECODABLE" in a:
                undecodable += 1
                continue
            if ix["name"] == "initialize_cubic_pool":
                st.weights = list(a["normalized_weights"])
                st.vb = list(a["initial_virtual_balances"])
                st.actual = [0] * len(st.vb)  # actual reserves start at zero (program docs)
            elif ix["name"] == "set_range_manager":
                st.manager, st.enabled = a["new_manager"], a["enabled"]
            elif ix["name"] == "set_range_manager_config":
                st.cfg = {k: a[k] for k in st.cfg}
            elif ix["name"] == "range_manager_update":
                ts = next((e["timestamp"] for n, e in events if n == "RangeManagerUpdated" and e["pool"] == pool),
                          tx["blockTime"])
                pre = {"virtual_balances": st.vb, "weights": st.weights, "actual_balances": st.actual}
                obligations = check_update(st, ix, ts)
                rec = {
                    "protocol": "Coffer",
                    "verification_profile": "COFFER-RM-01",
                    "program": {"id": PROGRAM_ID, "idl": "cubic_pool 0.5.1", "idl_sha256": IDL_SHA256},
                    "pool": pool,
                    "transition": {"signature": sig, "slot": tx["slot"], "unix_timestamp": ts,
                                   "manager": ix["accounts"]["authority"]["key"], "tx_sha256": sha256_json(tx)},
                    "policy_in_effect": dict(st.cfg, range_manager=st.manager, enabled=st.enabled),
                    "pre_state": pre,
                    "action": {"vb_changes": a["vb_changes"], "weight_changes": a["weight_changes"]},
                    "obligations": obligations,
                }
                rec["final"] = final_result(obligations)
                records.append(rec)
                for c in a["vb_changes"]:
                    if st.vb is not None:
                        st.vb[c["index"]] = c["new_value"]
                for c in a["weight_changes"]:
                    if st.weights is not None:
                        st.weights[c["index"]] = c["new_value"]
                st.last_update = ts
        for n, e in events:
            if n == "PoolStateLog" and e["pool"] == pool:
                st.vb, st.actual = list(e["virtual_balances"]), list(e["actual_balances"])
    return {"pool": pool, "records": records, "rejected_updates": rejected,
            "undecodable_items": undecodable,
            "final_state": {"weights": st.weights, "virtual_balances": st.vb, "actual_balances": st.actual,
                            "range_manager": st.manager, "enabled": st.enabled, "config": st.cfg,
                            "last_update": st.last_update}}
