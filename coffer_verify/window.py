"""Fetch every successful transaction of every Coffer pool in a time window
(read-only, resumable through the local cache)."""

import json
import sys
import time
from pathlib import Path

from .chain import PROGRAM_ID, fetch_transaction, rpc, signatures

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "tx"


def pools() -> list:
    accs = rpc("getProgramAccounts", [PROGRAM_ID, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0},
                                                   "filters": [{"dataSize": 1683}]}])
    return sorted(a["pubkey"] for a in accs)


def main(start: int, end: int) -> None:
    plan = []
    for p in pools():
        sigs = [s for s in signatures(p) if s["err"] is None and start <= (s.get("blockTime") or 0) < end]
        plan += [(p, s["signature"]) for s in sigs]
    out = ROOT / ".cache" / f"window-{start}-{end}.json"
    out.write_text(json.dumps(plan))
    print(f"{len(plan)} transactions in window", flush=True)
    for i, (_, sig) in enumerate(plan):
        fetch_transaction(sig, CACHE)
        if i % 500 == 0:
            print(f"  {i}/{len(plan)}  {time.strftime('%H:%M')}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]))
