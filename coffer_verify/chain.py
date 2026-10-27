"""Read-only access to Solana and decoding of Coffer cubic_pool transactions.

Authoritative source: the Solana ledger (getTransaction). The Coffer API and
router are never used as evidence - they are indexing/presentation layers.
"""

import base64
import json
import time
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .borsh import B58, Dec

V6_PROGRAM = "6DhW1Ek5WLJSnFSpBBGYeVRbRiWtCXHbqyGJ6LcVZgH9"
# v6 (RWA index pools) adds trailing event fields (Swap transfer fees, PoolInitialized pool_id,
# RangeManagerConfigSet min leverage); IDL from coffer-so/sdk branch feature/sdk-v6 (MIT).
IDL_PATH = Path(__file__).resolve().parent / "idl" / (
    "cubic_pool-0.5.1-v6.json" if os.environ.get("COFFER_PROGRAM") == V6_PROGRAM else "cubic_pool-0.5.1.json")
IDL = json.loads(IDL_PATH.read_text())
PROGRAM_ID = IDL["address"]   # COFFER_PROGRAM selects v5 (default) or v6; run.py sets it from account.json
DEC = Dec(IDL)
EVENTS = {bytes(e["discriminator"]): e["name"] for e in IDL["events"]}
INSTRUCTIONS = {bytes(i["discriminator"]): i for i in IDL["instructions"]}
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def rpc(method: str, params: list, url: str = DEFAULT_RPC) -> Any:
    err: Optional[Exception] = None
    for attempt in range(8):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        try:
            reply = json.load(urllib.request.urlopen(req, timeout=60))
            if "error" in reply:
                raise RuntimeError(reply["error"])
            return reply["result"]
        except Exception as e:  # rate limits on public RPC: back off and retry
            err = e
            time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"{method} failed: {err}")


def signatures(address: str, url: str = DEFAULT_RPC) -> List[Dict[str, Any]]:
    """All signatures touching `address`, oldest first."""
    out, before = [], None
    while True:
        opts: Dict[str, Any] = {"limit": 1000}
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [address, opts], url)
        out += page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    return list(reversed(out))


def account_snapshot(address: str, url: str = DEFAULT_RPC) -> Dict[str, Any]:
    """Current account data with the slot it was read at (for the final-state cross-check)."""
    r = rpc("getAccountInfo", [address, {"encoding": "base64", "commitment": "finalized"}], url)
    return {"slot": r["context"]["slot"], "data": r["value"]["data"][0], "owner": r["value"]["owner"]}


def fetch_transaction(sig: str, cache: Path, url: str = DEFAULT_RPC) -> Dict[str, Any]:
    path = cache / f"{sig}.json"
    if path.exists():
        return json.loads(path.read_text())
    tx = rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1}], url)
    if tx is None:  # not yet served by the node; never cache an empty answer
        raise RuntimeError(f"transaction {sig} not available yet")
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tx, sort_keys=True))
    return tx


def account_keys(tx: Dict[str, Any]) -> List[str]:
    keys = list(tx["transaction"]["message"]["accountKeys"])
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable", [])) + list(loaded.get("readonly", []))


def decode_events(tx: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Anchor events from 'Program data:' logs emitted by the cubic_pool program
    itself (tracked through the invoke stack - other Anchor programs in the same
    transaction can emit events with the same name and discriminator). Events
    that do not decode with the pinned IDL are reported as ('UNDECODABLE', ...)."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    stack: List[str] = []
    for line in (tx.get("meta") or {}).get("logMessages") or []:
        parts = line.split(" ")
        if line.startswith("Program ") and len(parts) >= 3 and parts[2] == "invoke":
            stack.append(parts[1])
            continue
        if line.startswith("Program ") and len(parts) >= 3 and parts[2] in ("success", "failed:"):
            if stack:
                stack.pop()
            continue
        if not line.startswith("Program data: ") or not stack or stack[-1] != PROGRAM_ID:
            continue
        raw = base64.b64decode(line[len("Program data: "):])
        name = EVENTS.get(raw[:8])
        if not name:
            continue
        try:
            value, _ = DEC.struct(name, raw, 8)
            out.append((name, value))
        except Exception:
            out.append(("UNDECODABLE", {"event": name, "bytes": len(raw)}))
    return out


def decode_instructions(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Top-level and inner cubic_pool instructions with decoded args and accounts."""
    keys = account_keys(tx)
    msg = tx["transaction"]["message"]
    n_signers = msg["header"]["numRequiredSignatures"]
    raw_ixs = list(msg["instructions"])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        raw_ixs += inner["instructions"]
    out = []
    for ix in raw_ixs:
        if keys[ix["programIdIndex"]] != PROGRAM_ID:
            continue
        data = b58decode(ix["data"])
        spec = INSTRUCTIONS.get(data[:8])
        if not spec:
            continue
        args, o = {}, 8
        try:
            for a in spec["args"]:
                args[a["name"]], o = DEC.read(a["type"], data, o)
        except Exception:
            args = {"UNDECODABLE": True}
        accounts = {
            spec["accounts"][i]["name"]: {"key": keys[k], "signer": k < n_signers}
            for i, k in enumerate(ix["accounts"])
            if i < len(spec["accounts"])
        }
        out.append({"name": spec["name"], "args": args, "accounts": accounts})
    return out
