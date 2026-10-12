"""The integer port of Coffer's swap math reproduces real on-chain swaps exactly."""

from coffer_verify import math as M

# Pool HwDbGNHp..., swap at slot 452298348: pre-state = PoolStateLog of the swap at slot 452297080.
PRE_VB = [2393682357, 20400000000, 21087997939]
PRE_ACTUAL = [73682357, 680000000, 729997939]
WEIGHTS = [2500, 2500, 5000]
SWAP_FEE_RATE, PROTOCOL_FEE_RATE = 10000, 2000


def test_real_swap_reproduced_to_the_unit():
    amount_in, i, o = 28492128, 2, 0
    fee = M.swap_fee(amount_in, SWAP_FEE_RATE)
    pfee = M.protocol_fee(fee, PROTOCOL_FEE_RATE)
    out = M.calc_out_given_in(PRE_VB[i], WEIGHTS[i], PRE_VB[o], WEIGHTS[o], amount_in - fee, PRE_ACTUAL[o])
    assert (fee, pfee, out) == (284922, 56985, 6390731)
    assert PRE_VB[o] - out == 2387291626  # next PoolStateLog virtual balance of token 0


def test_fees_round_up():
    assert M.swap_fee(1, 3000) == 1
    assert M.protocol_fee(1, 1) == 1


def test_amount_out_never_exceeds_actual_reserves():
    import pytest
    with pytest.raises(M.MathError):
        M.calc_out_given_in(10**12, 5000, 10**12, 5000, 10**12, 1)


def test_js_style_truncation():
    assert M.tdiv(-7, 2) == -3 and M.tmod(-7, 2) == -1
