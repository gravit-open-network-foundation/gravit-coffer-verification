"""Adversarial cases: each must be caught by exactly the obligation it violates."""

import copy

from coffer_verify.verify import PoolState, check_update, final_result

MANAGER = "Manager1111111111111111111111111111111111111"


def state():
    st = PoolState()
    st.manager, st.enabled = MANAGER, True
    st.cfg = {"max_vb_change_pct": 2000, "max_weight_change_pct": 1000, "min_update_interval_secs": 3600,
              "max_leverage_bps": 200_000, "min_leverage_bps": 10_000}
    st.weights = [2500, 2500, 5000]
    st.vb = [1_000_000, 2_000_000, 4_000_000]
    st.actual = [100_000, 200_000, 400_000]
    st.last_update = 1_000
    return st


def update(vb=None, wt=None, signer=MANAGER, is_signer=True):
    return {"name": "range_manager_update",
            "args": {"vb_changes": vb or [], "weight_changes": wt or []},
            "accounts": {"authority": {"key": signer, "signer": is_signer}}}


def results(st, ix, ts=10_000):
    return {o["id"]: o["result"] for o in check_update(st, ix, ts)}


VALID = dict(vb=[{"index": 0, "expected_current": 1_000_000, "new_value": 850_000}],
             wt=[{"index": 0, "expected_current": 2500, "new_value": 2700},
                 {"index": 2, "expected_current": 5000, "new_value": 4800}])


def test_valid_transition_is_verified():
    r = results(state(), update(**VALID))
    assert set(r.values()) <= {"VERIFIED", "NOT_APPLICABLE"}
    assert final_result(check_update(state(), update(**VALID), 10_000)) == "VERIFIED"


def test_unauthorized_signer():
    assert results(state(), update(**VALID, signer="Attacker"))["COF-AUTH"] == "INVALID"


def test_manager_disabled():
    st = state(); st.enabled = False
    assert results(st, update(**VALID))["COF-AUTH"] == "INVALID"


def test_virtual_balance_change_over_cap():
    ix = update(vb=[{"index": 1, "expected_current": 2_000_000, "new_value": 1_500_000}])  # -25% > 20%
    assert results(state(), ix)["COF-VB"] == "INVALID"


def test_weight_change_relative_cap():
    # 2500 -> 2800 is +12% of the current value (> 10%), although only 3 percentage points
    ix = update(wt=[{"index": 0, "expected_current": 2500, "new_value": 2800},
                    {"index": 2, "expected_current": 5000, "new_value": 4700}])
    assert results(state(), ix)["COF-WT"] == "INVALID"


def test_weights_must_sum_to_scale():
    ix = update(wt=[{"index": 0, "expected_current": 2500, "new_value": 2600}])
    assert results(state(), ix)["COF-WT"] == "INVALID"


def test_too_frequent():
    assert results(state(), update(**VALID), ts=1_000 + 3599)["COF-INT"] == "INVALID"


def test_leverage_floor():
    # 4_000_000 / 400_000 = 10x; band 1x..20x. Push token 2 to 0.5x is caught by both VB cap and floor
    st = state(); st.cfg["max_vb_change_pct"] = 10_000
    ix = update(vb=[{"index": 2, "expected_current": 4_000_000, "new_value": 200_000}])
    assert results(st, ix)["COF-LEV"] == "INVALID"


def test_declared_pre_state_must_match_reconstruction():
    ix = update(vb=[{"index": 0, "expected_current": 999_999, "new_value": 900_000}])
    assert results(state(), ix)["COF-STATE"] == "CONFLICTING"


def test_check_does_not_mutate_state():
    st = state(); before = copy.deepcopy(st.__dict__)
    check_update(st, update(**VALID), 10_000)
    assert st.__dict__ == before
