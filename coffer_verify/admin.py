"""
Admin actions (profile COFFER-ADM-01): who changed what in a pool, and was it allowed.

Every successful cubic_pool instruction that changes a pool's settings is recorded in ledger
order, with its signer or authority. Two kinds:

- pool admin actions (fee rate, sell-off limit, range manager config, admin transfer): signed
  by the pool admin. The admin is followed from the ledger: the payer of initialize_cubic_pool,
  then whoever accepts a pending transfer.
- protocol actions (protocol fee, enable/disable, token activity, range manager, fee
  collection, withdrawals): carry the config account and are executed by the protocol
  authority, usually through the protocol_admin program. They are recorded with their
  authority; the authority's own key policy is outside this profile.

| ID | Obligation |
|---|---|
| COF-ADM-AUTH | a pool admin action is signed by the pool admin in office at that moment |
| COF-ADM-XFER | an admin transfer is accepted only by the admin named when it was initiated |
| COF-ADM-DEBUG | no debug or withdrawal function touched the pool (`debug_withdraw_liquidity`, `withdraw_sol`); a call is reported as ALERT |

Protocol actions are `RECORDED`: listed, not judged.
"""

from typing import Any, Dict, List

from .chain import decode_instructions

POOL_ADMIN = {"set_swap_fee_rate", "set_max_selloff", "set_range_manager_config", "disable_pool_admin",
              "initiate_pool_admin_transfer", "cancel_pool_admin_transfer"}
PROTOCOL = {"set_protocol_fee_rate", "set_pool_enabled", "set_token_active", "set_swaps_enabled",
            "set_range_manager", "collect_protocol_fees", "initialize_pool_alt"}
ALERT = {"debug_withdraw_liquidity", "withdraw_sol"}


def admin_actions(pool: str, txs: List[Dict[str, Any]]) -> Dict[str, Any]:
    admin, pending, records = None, None, []
    for tx in txs:
        if (tx.get("meta") or {}).get("err"):
            continue
        sig = tx["transaction"]["signatures"][0]
        for ix in decode_instructions(tx):
            accs, name = ix["accounts"], ix["name"]
            if accs.get("pool", {}).get("key") != pool and name not in ALERT:
                continue
            signer = next((a["key"] for k, a in accs.items() if k in ("authority", "new_admin", "payer")), None)
            rec = {"tx": sig, "slot": tx.get("slot"), "instruction": name, "args": ix["args"], "by": signer}
            if name == "initialize_cubic_pool":
                admin = accs["payer"]["key"]
                rec.update(id="COF-ADM-AUTH", result="RECORDED", detail="pool created; admin = payer")
            elif name == "accept_pool_admin_transfer":
                ok = pending is not None and signer == pending and accs["new_admin"]["signer"]
                rec.update(id="COF-ADM-XFER", result="VERIFIED" if ok else "INVALID",
                           detail=f"pending {pending}, accepted by {signer}")
                if ok:
                    admin, pending = signer, None
            elif name in POOL_ADMIN:
                if admin is None:
                    rec.update(id="COF-ADM-AUTH", result="INSUFFICIENT", detail="admin unknown: evidence starts after pool creation")
                else:
                    ok = signer == admin and accs["authority"]["signer"]
                    rec.update(id="COF-ADM-AUTH", result="VERIFIED" if ok else "INVALID", detail=f"admin {admin}")
                if name == "initiate_pool_admin_transfer" and rec["result"] != "INVALID":
                    pending = ix["args"].get("new_admin")
                elif name == "cancel_pool_admin_transfer" and rec["result"] != "INVALID":
                    pending = None
            elif name in ALERT:
                rec.update(id="COF-ADM-DEBUG", result="ALERT", detail="debug or withdrawal function called")
            elif name in PROTOCOL:
                rec.update(id="COF-ADM-PROTO", result="RECORDED", detail="protocol action through the config authority")
            else:
                continue
            records.append(rec)
    counts: Dict[str, Dict[str, int]] = {}
    for r in records:
        counts.setdefault(r["id"], {}).setdefault(r["result"], 0)
        counts[r["id"]][r["result"]] += 1
    alerts = sum(1 for r in records if r["result"] == "ALERT")
    return {"records": records, "summary": {"actions": len(records), "by_obligation": counts,
                                            "alerts": alerts, "admin_now": admin}}
