"""COFFER-ADM-01: admin actions follow the admin from the ledger; debug calls raise an alert."""
from coffer_verify import admin

POOL = "P"


def tx(sig, *ixs, err=None):
    return {"meta": {"err": err}, "slot": 1, "transaction": {"signatures": [sig]}, "_ixs": list(ixs)}


def ix(name, args=None, **accs):
    return {"name": name, "args": args or {}, "accounts": {k: {"key": v[0], "signer": v[1]} for k, v in accs.items()}}


def run(monkeypatch, txs):
    monkeypatch.setattr(admin, "decode_instructions", lambda t: t["_ixs"])
    return admin.admin_actions(POOL, txs)


def test_transfer_and_fee_change(monkeypatch):
    out = run(monkeypatch, [
        tx("a", ix("initialize_cubic_pool", pool=(POOL, False), payer=("A", True))),
        tx("b", ix("initiate_pool_admin_transfer", {"new_admin": "B"}, pool=(POOL, False), authority=("A", True))),
        tx("c", ix("accept_pool_admin_transfer", pool=(POOL, False), new_admin=("B", True))),
        tx("d", ix("set_swap_fee_rate", {"swap_fee_rate": 1}, pool=(POOL, False), authority=("B", True))),
    ])
    assert [r["result"] for r in out["records"]] == ["RECORDED", "VERIFIED", "VERIFIED", "VERIFIED"]
    assert out["summary"]["admin_now"] == "B"


def test_old_admin_after_transfer_is_invalid(monkeypatch):
    out = run(monkeypatch, [
        tx("a", ix("initialize_cubic_pool", pool=(POOL, False), payer=("A", True))),
        tx("b", ix("initiate_pool_admin_transfer", {"new_admin": "B"}, pool=(POOL, False), authority=("A", True))),
        tx("c", ix("accept_pool_admin_transfer", pool=(POOL, False), new_admin=("B", True))),
        tx("d", ix("set_swap_fee_rate", pool=(POOL, False), authority=("A", True))),
    ])
    assert out["records"][-1]["result"] == "INVALID"


def test_accept_by_someone_else_is_invalid(monkeypatch):
    out = run(monkeypatch, [
        tx("a", ix("initialize_cubic_pool", pool=(POOL, False), payer=("A", True))),
        tx("b", ix("initiate_pool_admin_transfer", {"new_admin": "B"}, pool=(POOL, False), authority=("A", True))),
        tx("c", ix("accept_pool_admin_transfer", pool=(POOL, False), new_admin=("X", True))),
    ])
    assert out["records"][-1]["result"] == "INVALID"


def test_debug_withdraw_is_alert_and_failed_tx_ignored(monkeypatch):
    out = run(monkeypatch, [
        tx("a", ix("debug_withdraw_liquidity", pool=(POOL, False), authority=("Z", False))),
        tx("b", ix("debug_withdraw_liquidity", pool=(POOL, False), authority=("Z", False)), err={"x": 1}),
    ])
    assert out["summary"]["alerts"] == 1
