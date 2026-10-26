# Gravit × Coffer — independent verification of programmable liquidity

**Experiments C-01 (profiles `COFFER-RM-01`, `COFFER-SWAP-01`) and C-02 (public metrics) · status: research, read-only**

[Coffer](https://docs.coffer.so) pools on Solana can be steered by a **range manager**: an
external actor (often automated) that changes virtual balances and token weights. The pool
admin declares an envelope the manager must stay within: how much it may change per update,
how often, and within which leverage band.

This repository checks, **independently of Coffer and after the fact**, that every real
range-manager transition on chain stayed within the policy that was in effect at that moment.
It uses only the Solana ledger. It does not use the Coffer API or router, which are indexing and
presentation layers, not evidence. Nothing in Coffer has to change.

> The question is not "is this smart contract secure?" (that is what an audit answers) but
> "did this autonomous actor do what its declared policy allows, and can anyone re-check it?"

This is **not a security audit** of Coffer and makes no claim about the safety of its code.

## What is checked

For every successful `range_manager_update` of a pool, in ledger order:

| ID | Obligation |
|---|---|
| COF-AUTH | the signer is the configured range manager, and the manager is enabled |
| COF-STATE | the pre-state the manager declared (`expected_current`) equals the state reconstructed independently from the ledger |
| COF-VB | each changed virtual balance moved by at most `max_vb_change_pct` of its current value (where actual reserves are positive) |
| COF-WT | resulting weights lie in [100, 9900] bps and sum to 10000; each change is at most `max_weight_change_pct` **of the current value** |
| COF-LEV | after the update every live token stays within `min_leverage_bps ≤ virtual / actual ≤ max_leverage_bps` |
| COF-INT | at least `min_update_interval_secs` passed since the previous successful update |

Each obligation is `VERIFIED`, `INVALID`, `NOT_APPLICABLE`, `INSUFFICIENT` (data missing) or
`CONFLICTING` (declared and reconstructed state disagree). Definitions follow the program's own
interface description (IDL `cubic_pool` 0.5.1, shipped in the MIT-licensed `@coffer_so/sdk`
1.3.1, vendored in `coffer_verify/idl/` with its licence); for example, leverage is
`virtual_balance / actual_balance`, and percentage caps are relative to the current value.

## Swaps and reserves (`COFFER-SWAP-01`)

Every swap is re-computed from the pool state just before it, with an exact integer port of
the program's math (`coffer_verify/math.py`, ported line by line from the MIT-licensed SDK):

| ID | Obligation |
|---|---|
| COF-PRICE | amount out equals the weighted constant-product result on virtual balances |
| COF-FEE | base fee (rounded up) and protocol share equal the event |
| COF-SURGE | sell-off window advanced and dynamic (surge) fee re-computed equal the event |
| COF-POST | predicted virtual, actual and protocol-fee balances equal the program's own post-state log |
| COF-RESERVE | each pool vault holds exactly actual balance + protocol fees owed |

Solana caps transaction logs; when a transaction's log is truncated, events after the cut
are lost. Such swaps are `INSUFFICIENT`, and the state is re-synchronised from the next log.

## Public metrics (C-02)

`python -m coffer_verify.metrics tvl` and `... volume START END` recompute TVL and swap volume
from the ledger. Token amounts are on-chain facts; USD values use Jupiter's public price API,
recorded raw next to each result. A price feed is not on-chain evidence, and the result says so.

## RWA index pools (C-03, first replay, 26 Oct 2026)

Coffer's RWA index pools run the same interface (IDL `cubic_pool` 0.5.1, shipped unchanged in
`@coffer_so/sdk` 1.4.0) under a new program, `6DhW1Ek5WLJSnFSpBBGYeVRbRiWtCXHbqyGJ6LcVZgH9`.
Set `COFFER_PROGRAM` to that address and the same checks apply. Three pools exist on it:

| Pool | Tokens | History | Result |
|---|---|---|---|
| `HGbQ74sgfvyhyEBgPwtKBge2HFcnZbHXTCSA2QdBTHA3` | NVDAx, GOOGLx, TSLAx, MSTRx, CRCLx | 2 liquidity adds, 5 swaps, pool admin transfer | 5 swaps VERIFIED, 20 reserve checks VERIFIED, final state matches chain |
| `4nKuFAyLARPCmSNZzdU3K9X4tfy3XvaZbQ4iqzKSx4Ta` | NVDAx, GOOGLx, TSLAx, MSTRx, CRCLx | no liquidity, enabled/disabled 4 times | final state matches chain |
| `Eyf6zRCBt98kEYoRUr1ZhgkHA1FjEkDC2ot4ySnvWMKq` | NVDAx, GOOGLx, METAx, MSFTx, TSLAx, AMZNx | 2 liquidity adds, no swaps yet | 12 reserve checks VERIFIED, final state matches chain |

These pools run on contracts that Coffer marks as not yet audited. This replay is not an audit;
it checks that what happened on chain agrees with the pool's own rules. Next: rules for admin
actions (who changed fees, who became admin, whether any debug or withdrawal function was
called) and a continuous observer with alerts.

```bash
COFFER_PROGRAM=6DhW1Ek5WLJSnFSpBBGYeVRbRiWtCXHbqyGJ6LcVZgH9 python3 -m coffer_verify.run verify HGbQ74sgfvyhyEBgPwtKBge2HFcnZbHXTCSA2QdBTHA3
```

## A second operator

See [OPERATORS.md](OPERATORS.md): re-run, sign, and let the Gravit Verifier decide. Two
independent operators make a Verified Fact; Coffer's own signature is recorded as the verified
party's acknowledgement, not as independent evidence.

## Reproduce

```bash
python3 -m coffer_verify.run verify <POOL>   # from the committed evidence only, no network
python3 -m pytest -q                         # adversarial cases
python3 -m coffer_verify.run fetch <POOL>    # optional: re-download evidence from Solana
```

`evidence/<pool>/transactions.jsonl.gz` holds every transaction of the pool, as returned by
Solana `getTransaction`. `results/<pool>/records.jsonl` holds one verification record per
transition, hash-chained (each entry carries the hash of the previous one), and
`summary.json` the totals. CI regenerates the results from the evidence and fails if a single
byte differs.

## Limits (stated plainly)

- Rules are bound to program version 0.5.1. Events of earlier program versions do not decode
  with this IDL and are counted as `undecodable_items`, not guessed.
- The exact integer rounding of the on-chain checks is not published; comparisons here are
  exact rational ones. A transition exactly at a cap boundary could differ by one unit.
- One checker is one opinion. In Gravit terms this gives a *Probabilistic Assessment*; a
  *Verified Fact* needs a second, independent operator to re-run the check and sign the result.
- Not covered yet: emergency governance state machine; add/remove liquidity amounts; the
  sell-off window is re-synchronised (not modelled) after liquidity changes.
