"""Exact integer port of Coffer swap math, line by line from the MIT-licensed
@coffer_so/sdk 1.3.1 (src/math/*.ts, src/clients/quote-math.ts), which itself
ports the on-chain Rust (cubic_math.rs, log_exp_math.rs, swap.rs).

JavaScript BigInt division truncates toward zero and `%` keeps the sign of the
dividend; Python's // and % floor. `tdiv`/`tmod` reproduce the JS semantics
wherever an operand can be negative.
"""

from typing import Dict, Optional

ONE = 10**18
U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
I128_MIN, I128_MAX = -(1 << 127), (1 << 127) - 1
SWAP_FEE_PRECISION = 1_000_000
PROTOCOL_FEE_PRECISION = 10_000
SCALE = 10_000


class MathError(ValueError):
    pass


def tdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def tmod(a: int, b: int) -> int:
    return a - b * tdiv(a, b)


def u64(v: int) -> int:
    if not 0 <= v <= U64_MAX:
        raise MathError("outside u64")
    return v


def u128(v: int) -> int:
    if not 0 <= v <= U128_MAX:
        raise MathError("outside u128")
    return v


def i128(v: int) -> int:
    if not I128_MIN <= v <= I128_MAX:
        raise MathError("outside i128")
    return v


# --- fixedPoint.ts -----------------------------------------------------------
def mul_div_down(a: int, b: int, d: int) -> int:
    u128(a); u128(b); u128(d)
    if d == 0:
        raise MathError("divide by zero")
    return u128(a * b // d)


def mul_down(a: int, b: int) -> int:
    return u128(u128(a) * u128(b)) // ONE


def div_down(a: int, b: int) -> int:
    u128(a); u128(b)
    if b == 0:
        raise MathError("divide by zero")
    return u128(a * ONE) // b


def div_up(a: int, b: int) -> int:
    u128(a); u128(b)
    if b == 0:
        raise MathError("divide by zero")
    n = u128(a * ONE)
    return 0 if n == 0 else (n - 1) // b + 1


def complement(x: int) -> int:
    if x > ONE:
        raise MathError("complement > ONE")
    return ONE - u128(x)


def weight_to_fp(w: int) -> int:
    return u64(w) * ONE // 10_000


# --- logExp.ts ---------------------------------------------------------------
MAX_NATURAL_EXPONENT = 46 * ONE
MIN_NATURAL_EXPONENT = -41 * ONE
X = [32 * ONE, 16 * ONE, 8 * ONE, 4 * ONE, 2 * ONE, ONE, ONE // 2, ONE // 4, ONE // 8, ONE // 16]
A = [78962960182680695161000000000000, 8886110520507872636760000, 2980957987041728274740,
     54598150033144239078, 7389056098930650227, 2718281828459045235, 1648721270700128146,
     1284025416687741484, 1133148453066826316, 1064494458917859429]


def _ln_positive(a: int) -> int:
    s = 0
    for xi, ai in zip(X, A):
        if a >= ai:
            a = mul_div_down(a, ONE, ai)
            s += xi
    z = mul_div_down(a - ONE, ONE, u128(a + ONE))
    z_sq = tdiv(i128(z * z), ONE)
    num = series = z
    for k in range(1, 6):
        num = tdiv(i128(num * z_sq), ONE)
        series = i128(series + tdiv(num, 2 * k + 1))
    return i128(s + i128(series * 2))


def ln_fp(a: int) -> int:
    u128(a)
    if a == 0:
        raise MathError("ln(0)")
    return -_ln_positive(mul_div_down(ONE, ONE, a)) if a < ONE else _ln_positive(a)


def exp_fp(x: int) -> int:
    i128(x)
    if x < MIN_NATURAL_EXPONENT or x > MAX_NATURAL_EXPONENT:
        raise MathError("exp out of range")
    if x < 0:
        return mul_div_down(ONE, ONE, exp_fp(-x))
    product = ONE
    for xi, ai in zip(X, A):
        if x >= xi:
            x -= xi
            product = mul_div_down(product, ai, ONE)
    s = u128(ONE + x)
    term = x
    for n in range(2, 13):
        term = mul_div_down(term, x, ONE) // n
        s = u128(s + term)
    return mul_div_down(product, s, ONE)


def pow_fp(base: int, exponent: int) -> int:
    u128(base); u128(exponent)
    if exponent == 0 or base == ONE:
        return ONE
    if base == 0:
        return 0
    lg = ln_fp(base)
    y = exponent if exponent < (1 << 127) else exponent - (1 << 128)
    whole = i128(tdiv(lg, ONE) * y)
    fraction = tdiv(i128(tmod(lg, ONE) * y), ONE)
    return exp_fp(i128(whole + fraction))


# --- cubicMath.ts / slippage.ts ------------------------------------------------
def calc_out_given_in(vb_in: int, w_in: int, vb_out: int, w_out: int, amount_in: int, actual_out: int) -> int:
    for v in (vb_in, w_in, vb_out, w_out, amount_in, actual_out):
        u64(v)
    base = div_up(vb_in, u128(vb_in + amount_in))
    exp = div_down(weight_to_fp(w_in), weight_to_fp(w_out))
    power = min(pow_fp(base, exp) + 1, ONE)
    out = mul_down(vb_out, complement(power))
    if out > actual_out:
        raise MathError("amount out exceeds actual balance")
    return u64(out)


def _ceil_ratio(amount: int, rate: int, scale: int) -> int:
    n = amount * rate
    return n // scale + (0 if n % scale == 0 else 1)


def swap_fee(amount: int, rate: int) -> int:
    return u64(_ceil_ratio(u64(amount), rate, SWAP_FEE_PRECISION))


def protocol_fee(fee: int, rate: int) -> int:
    return u64(_ceil_ratio(u64(fee), rate, PROTOCOL_FEE_PRECISION))


# --- maxSelloff.ts (checkAndAdvanceSelloff) -------------------------------------
def advance_selloff(st: Dict[str, int], max_pct: int, period: int, amount_in: int, vb: int, now: int) -> Optional[Dict[str, int]]:
    if max_pct == 0:
        return None
    p = period
    elapsed = max(now - st["window_start_timestamp"], 0)
    prev, cur, start, opened = st["previous_selloff"], st["current_selloff"], st["window_start_timestamp"], False
    if elapsed >= 2 * p:
        prev, cur, start, elapsed, opened = 0, 0, now, 0, True
    elif elapsed >= p:
        prev, cur, start = cur, 0, start + p
        elapsed = max(now - start, 0)
        opened = True
    snap = vb if (opened or st["selloff_vb_snapshot"] == 0) else st["selloff_vb_snapshot"]
    if opened and st["selloff_vb_snapshot"] > 0 and snap != st["selloff_vb_snapshot"]:
        prev = u64(u128(prev * snap) // st["selloff_vb_snapshot"])
    cap = u64(max_pct * snap // 10_000)
    before = prev * (p - elapsed) // p + cur
    effective = before + amount_in
    if effective > cap:
        raise MathError("MaxSelloffExceeded")
    return {"before": before, "effective": effective, "cap": cap, "selloff_vb_snapshot": snap,
            "previous_selloff": prev, "current_selloff": u64(cur + amount_in), "window_start_timestamp": start}


# --- surgeFee.ts -------------------------------------------------------------------
def _sub(a: int, b: int) -> int:
    return a - b if a > b else 0


def _ceil(a: int, b: int) -> int:
    return a // b + (0 if a % b == 0 else 1)


def _kink_t(c: Dict[str, int]) -> int:
    thr, fill = c["threshold"], c["kink"] * 100
    if c["kink"] == 0 or thr >= SCALE or fill <= thr or fill >= SCALE:
        return 0
    return (fill - thr) * ONE // (SCALE - thr)


def _rate_at(t: int, c: Dict[str, int], k: int) -> int:
    t = min(t, ONE)
    lo, mid, hi = c["low"], c["mid"], c["high"]
    if k == 0:
        return min(lo + _ceil(_sub(hi, lo) * t, ONE), SCALE)
    if t <= k:
        return min(lo + _ceil(_sub(mid, lo) * t, k), SCALE)
    return min(mid + _ceil(_sub(hi, mid) * (t - k), ONE - k), SCALE)


def _integral_to(t: int, c: Dict[str, int], k: int) -> int:
    t = min(t, ONE)
    lo, mid, hi = c["low"], c["mid"], c["high"]

    def ramp(x: int, span: int, den: int) -> int:
        return 0 if den == 0 or span == 0 or x == 0 else span * (x * x // (2 * den))

    if k == 0:
        return lo * t + ramp(t, _sub(hi, lo), ONE)
    if t <= k:
        return lo * t + ramp(t, _sub(mid, lo), k)
    rest = t - k
    return k * (lo + mid) // 2 + mid * rest + ramp(rest, _sub(hi, mid), ONE - k)


def surge_fee_pct(c: Dict[str, int], before: int, after: int, cap: int) -> int:
    thr = c["threshold"]
    if cap == 0 or c["high"] == 0 or thr >= SCALE:
        return 0
    k = _kink_t(c)
    f0, f1 = min(before * SCALE // cap, SCALE), min(after * SCALE // cap, SCALE)
    if f1 <= thr:
        return 0
    den = SCALE - thr
    t1 = (f1 - thr) * ONE // den
    if f1 <= f0:
        return _rate_at(t1, c, k)
    f0c = f0 if f0 > thr else thr
    t0 = (f0c - thr) * ONE // den
    integral = _sub(_integral_to(t1, c, k), _integral_to(t0, c, k))
    return min(_ceil(den * integral, (f1 - f0c) * ONE), SCALE)


def surge_fee_amount(c: Dict[str, int], window: Optional[Dict[str, int]], in_after_fee: int, amount_out: int,
                     vb_in: int, w_in: int, vb_out: int, w_out: int, actual_out: int) -> int:
    if not window:
        return 0
    before, after, cap = window["before"], window["effective"], window["cap"]
    if surge_fee_pct(c, before, after, cap) == 0:
        return 0
    thr_units = cap * c["threshold"] // SCALE
    span, taxed_lo = _sub(after, before), max(before, thr_units)
    if span == 0 or after <= taxed_lo:
        return 0

    def cumulative(u: int) -> int:
        if u <= before:
            return 0
        if u >= after:
            return amount_out
        return calc_out_given_in(vb_in, w_in, vb_out, w_out, in_after_fee * (u - before) // span, actual_out)

    width = after - taxed_lo
    fee, prev_u, prev_y = 0, taxed_lo, cumulative(taxed_lo)
    for k in range(1, 5):
        u = after if k == 4 else taxed_lo + width * k // 4
        y = cumulative(u)
        seg = _sub(y, prev_y)
        if seg > 0 and u > prev_u:
            fee += _ceil(seg * surge_fee_pct(c, prev_u, u, cap), SCALE)
        prev_u, prev_y = u, y
    return min(fee, amount_out)
