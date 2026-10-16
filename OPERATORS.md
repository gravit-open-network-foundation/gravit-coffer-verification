# Re-run and sign C-01 — guide for a second operator

One checker is one opinion. Under the Gravit rules a result becomes a **Verified Fact** only
when at least **two independent operators** have re-run it and signed the same result. This
page is everything a second operator needs. It takes about ten minutes and needs no access to
Gravit or Coffer systems.

## 1. Re-run

```bash
git clone <this repository> && cd gravit-coffer-verification
python3.12 -m venv .venv
git clone --depth 1 --branch v0.6.0 https://github.com/gravit-open-network-foundation/verifier.git .deps/verifier
.venv/bin/pip install -e .deps/verifier pytest
.venv/bin/python -m pytest -q                       # adversarial and math tests
for d in evidence/*/; do [ -f "$d/transactions.jsonl.gz" ] && .venv/bin/python -m coffer_verify.run verify "$(basename "$d")"; done
git status --short results/                         # must print nothing: you reproduced every byte
```

Optional, and the strongest check: re-download the evidence yourself from any Solana RPC node
(`python -m coffer_verify.run fetch <pool>`) instead of trusting the committed files.

## 2. Create your key (once)

```bash
.venv/bin/python -m coffer_verify.attest keygen "<your legal entity name>"
```

The private key is written to `~/.gravit-operator/` on your machine and never leaves it. The
command prints your `key_id`, public key and a proof-of-possession signature. To be listed in
the Gravit verifier registry, send these with the documents in the registry policy
(`registry/POLICY.md` of the verifier: legal entity, contact, independence statement).

## 3. Sign

```bash
.venv/bin/python -m coffer_verify.attest sign "<your legal entity name>" ~/.gravit-operator/<file>.pem --relation independent
```

`sign` re-runs everything and refuses to sign if a single result byte differs from the
committed one. It writes `attestations/<name>.json`: a Gravit evidence record (Ed25519) over the
SHA-256 digest of the complete evidence and results bundle. Open a pull request with that file.

## 4. Decision

```bash
.venv/bin/python -m coffer_verify.attest decide <registry.json> <trust_root.json>
```

The public Gravit Verifier v0.6.0 decides. With signed records from two operators that are
listed in the registry and not under common control, the result is **Verified Fact**; with
one, it stays a *Probabilistic Assessment*.

## The verified party

Coffer may run steps 1–3 with `--relation subject`. That confirms Coffer reproduces the same
result from the same ledger data, which is valuable, and it is recorded. It is **not** counted
as independent evidence: a protocol cannot independently verify itself. Independent operators
are parties with no common control with Coffer or with each other.

## Current status

- Gravit's reference run: done, results committed. Signature with a **test key** until the
  Foundation's root key ceremony is completed; then it is re-signed with a registered key.
- Second independent operator: open.
