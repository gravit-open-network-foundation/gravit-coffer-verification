#!/usr/bin/env python3
"""
C-01 runner.

  python -m coffer_verify.run fetch  POOL   # read-only: ledger -> evidence/<pool>/transactions.jsonl.gz
  python -m coffer_verify.run verify POOL   # evidence -> results/<pool>/records.jsonl + summary.json

`verify` uses only the committed evidence file, never the network, so anyone can
reproduce the results byte for byte; CI does exactly that.
"""

import gzip
import io
import json
import os
import sys
from pathlib import Path

import base64

# The program (v5 or v6) is chosen when .chain is imported. For `verify`, take it from the
# committed account snapshot, so that a plain loop over evidence/ needs no settings.
if __name__ == "__main__" and len(sys.argv) > 2 and "COFFER_PROGRAM" not in os.environ:
    _snap = Path(__file__).resolve().parent.parent / "evidence" / sys.argv[2] / "account.json"
    if _snap.exists():
        os.environ["COFFER_PROGRAM"] = json.loads(_snap.read_text()).get("owner", "")

from .admin import admin_actions
from .chain import DEC, account_snapshot, fetch_transaction, signatures
from .swaps import verify_swaps
from .verify import replay, sha256_json

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "tx"


def evidence_path(pool: str) -> Path:
    return ROOT / "evidence" / pool / "transactions.jsonl.gz"


def fetch(pool: str, since: int = 0) -> None:
    """since = 0: the pool's complete history; otherwise every transaction with blockTime >= since."""
    snap = account_snapshot(pool)  # read first: every transaction up to this slot is in the history below
    sigs = [s for s in signatures(pool) if s["slot"] <= snap["slot"] and (s.get("blockTime") or 0) >= since
            and (since == 0 or s["err"] is None)]  # windowed evidence: successful transactions only
    snap["evidence_from_unix_time"] = since
    txs = []
    for i, s in enumerate(sigs):
        txs.append(fetch_transaction(s["signature"], CACHE))
        if i % 100 == 0:
            print(f"  {i}/{len(sigs)}", flush=True)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for tx in txs:
            gz.write((json.dumps(tx, sort_keys=True, separators=(",", ":")) + "\n").encode())
    p = evidence_path(pool)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(buf.getvalue())
    (p.parent / "account.json").write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    print(f"{pool}: {len(txs)} transactions up to slot {snap['slot']} -> {p.relative_to(ROOT)}")


POOL_LEN = 1683  # CubicPool::LEN since v5; pools never migrated keep the older, shorter layout


def legacy_layout(snap: dict) -> bool:
    return len(base64.b64decode(snap["data"])) < POOL_LEN


def load(pool: str):
    with gzip.open(evidence_path(pool), "rt") as f:
        return [json.loads(line) for line in f]


def cross_check(pool: str, fs: dict) -> dict:
    """Does the state reconstructed from the history equal the account read from the chain?"""
    path = evidence_path(pool).parent / "account.json"
    if not path.exists():
        return {"result": "INSUFFICIENT", "detail": "no account snapshot"}
    snap = json.loads(path.read_text())
    if legacy_layout(snap):
        return {"result": "INSUFFICIENT", "detail": "account in the pre-v5 layout (never migrated); not decodable with IDL 0.5.1"}
    acc, _ = DEC.struct("CubicPool", base64.b64decode(snap["data"]), 8)
    n = acc["token_count"]
    chain = {
        "weights": [t["config"]["normalized_weight"] for t in acc["tokens"][:n]],
        "virtual_balances": [t["dynamics"]["virtual_balance"] for t in acc["tokens"][:n]],
        "actual_balances": [t["dynamics"]["actual_balance"] for t in acc["tokens"][:n]],
        "range_manager": acc["range_manager"],
        "enabled": acc["range_manager_enabled"],
        "last_update": acc["range_manager_last_updated"],
        "config": {"max_vb_change_pct": acc["range_manager_max_vb_change_pct"],
                   "max_weight_change_pct": acc["range_manager_max_weight_change_pct"],
                   "min_update_interval_secs": acc["range_manager_min_update_interval_secs"],
                   "max_leverage_bps": acc["range_manager_max_leverage_bps"],
                   "min_leverage_bps": acc["range_manager_min_leverage_bps"]},
    }
    # a field the replay could not rebuild (creation event in an older, undecodable layout) is a gap, not a conflict
    missing = sorted(k for k in chain if fs.get(k) is None)
    diff = {k: {"reconstructed": fs.get(k), "chain": v} for k, v in chain.items() if k not in missing and fs.get(k) != v}
    result = "CONFLICTING" if diff else "INSUFFICIENT" if missing else "VERIFIED"
    out = {"result": result, "snapshot_slot": snap["slot"], "differences": diff}
    if missing:
        out["not_reconstructed"] = missing
    return out


def write_chain(path: Path, records: list) -> str:
    """Hash-chained JSONL: each entry carries the hash of the previous one."""
    prev = "0" * 64
    lines = []
    for i, rec in enumerate(records):
        entry = {"seq": i, "prev_hash": prev, "record": rec}
        entry["entry_hash"] = sha256_json(entry)
        prev = entry["entry_hash"]
        lines.append(json.dumps(entry, sort_keys=True))
    path.write_text("".join(l + "\n" for l in lines))
    return prev


def verify(pool: str) -> int:
    txs = load(pool)
    acc_path = evidence_path(pool).parent / "account.json"
    snap = json.loads(acc_path.read_text()) if acc_path.exists() else None
    from_genesis = not snap or not snap.get("evidence_from_unix_time")
    legacy = bool(snap) and legacy_layout(snap)
    acc = DEC.struct("CubicPool", base64.b64decode(snap["data"]), 8)[0] if snap and not legacy else None
    out = replay(pool, txs, acc, from_genesis)
    d = ROOT / "results" / pool
    d.mkdir(parents=True, exist_ok=True)
    prev = write_chain(d / "records.jsonl", out["records"])
    sw = verify_swaps(pool, txs, json.loads(acc_path.read_text())["data"]) if acc_path.exists() and not legacy else None
    swap_summary = None
    if sw is not None:
        by_ob: dict = {}
        for r in sw["swaps"]:
            for o in r["obligations"]:
                by_ob.setdefault(o["id"], {}).setdefault(o["result"], 0)
                by_ob[o["id"]][o["result"]] += 1
        finals: dict = {}
        for r in sw["swaps"]:
            finals[r["final"]] = finals.get(r["final"], 0) + 1
        swap_summary = {"swaps": len(sw["swaps"]), "results": finals, "by_obligation": by_ob,
                        "reserve_checks": sw["reserves"],
                        "chain_head": write_chain(d / "swaps.jsonl", sw["swaps"])}
    counts = {}
    for rec in out["records"]:
        counts[rec["final"]] = counts.get(rec["final"], 0) + 1
    final_check = cross_check(pool, out["final_state"]) if from_genesis else {
        "result": "NOT_APPLICABLE", "detail": "evidence starts after pool creation; no full replay"}
    summary = {
        "final_state_matches_chain": final_check,
        "pool": pool,
        "evidence": {"file": str(evidence_path(pool).relative_to(ROOT)), "transactions": len(txs),
                     "from": "pool creation" if from_genesis else snap["evidence_from_unix_time"],
                     "first_slot": txs[0]["slot"] if txs else None, "last_slot": txs[-1]["slot"] if txs else None},
        "range_manager_updates": len(out["records"]),
        "swap_verification": swap_summary,
        "results": counts,
        "rejected_by_program": out["rejected_updates"],
        "undecodable_items": out["undecodable_items"],
        "chain_head": prev,
        "final_state": out["final_state"],
    }
    adm = admin_actions(pool, txs)
    summary["admin_actions"] = dict(adm["summary"], chain_head=write_chain(d / "admin.jsonl", adm["records"]))
    (d / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: summary[k] for k in ("pool", "range_manager_updates", "results", "undecodable_items")}))
    print("  final state matches chain:", final_check["result"], final_check.get("differences") or "")
    if swap_summary:
        print("  swaps:", swap_summary["swaps"], swap_summary["results"], "reserves:",
              {k: v for k, v in swap_summary["reserve_checks"].items() if k != "examples"})
    print("  admin actions:", adm["summary"]["actions"], adm["summary"]["by_obligation"], "alerts:", adm["summary"]["alerts"])
    for rec in out["records"]:
        print(f"  slot {rec['transition']['slot']}  {rec['final']:12}  "
              + " ".join(f"{o['id']}={o['result']}" for o in rec["obligations"]))
    return 0


if __name__ == "__main__":
    cmd, pool = sys.argv[1], sys.argv[2]
    since = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    raise SystemExit(fetch(pool, since) if cmd == "fetch" else verify(pool))
