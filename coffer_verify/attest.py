#!/usr/bin/env python3
"""
Second-operator kit: re-run C-01, sign the result, and let the Gravit Verifier decide.

  python -m coffer_verify.attest keygen  NAME             # once: key stays on YOUR machine
  python -m coffer_verify.attest sign    NAME KEYFILE --relation independent|subject
  python -m coffer_verify.attest decide  REGISTRY TRUST_ROOT

`sign` regenerates every result from the committed evidence and refuses to sign
unless they are byte-identical to the committed ones; it then signs one
Gravit evidence record over the digest of the whole evidence + results bundle.

`decide` feeds the attestations to the public Gravit Verifier (v0.5.0) and asks
for "Verified Fact". Profile rule: an attestation by the verified party itself
(--relation subject, e.g. Coffer) confirms reproducibility but is NOT counted as
independent evidence - a protocol cannot independently verify itself.
"""

import argparse
import base64
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_ID = "coffer-c01-COFFER-RM-01+COFFER-SWAP-01"
KEY_DIR = Path.home() / ".gravit-operator"


def bundle_digest() -> str:
    """SHA-256 over every evidence and result file (path + content hash), in sorted order."""
    h = hashlib.sha256()
    for base in ("evidence", "results"):
        for p in sorted((ROOT / base).rglob("*")):
            if p.is_file():
                h.update(f"{p.relative_to(ROOT)}\n{hashlib.sha256(p.read_bytes()).hexdigest()}\n".encode())
    h.update(hashlib.sha256((ROOT / "coffer_verify" / "idl" / "cubic_pool-0.5.1.json").read_bytes()).hexdigest().encode())
    return h.hexdigest()


def regenerate_and_compare() -> dict:
    """Re-run every pool from evidence; fail if any result byte differs. Returns totals."""
    from .run import verify

    before = {p: p.read_bytes() for p in (ROOT / "results").rglob("*") if p.is_file()}
    for d in sorted((ROOT / "evidence").iterdir()):
        if (d / "transactions.jsonl.gz").exists():
            verify(d.name)
    after = {p: p.read_bytes() for p in (ROOT / "results").rglob("*") if p.is_file()}
    if before != after:
        changed = sorted(str(p.relative_to(ROOT)) for p in set(before) | set(after) if before.get(p) != after.get(p))
        sys.exit(f"results differ from the committed ones, refusing to sign: {changed}")
    totals = {"VERIFIED": 0, "INVALID": 0, "CONFLICTING": 0, "INSUFFICIENT": 0, "NOT_APPLICABLE": 0}
    for f in (ROOT / "results").glob("*/*.jsonl"):
        for line in f.read_text().splitlines():
            for o in json.loads(line)["record"]["obligations"]:
                totals[o["result"]] += 1
    return totals


def keygen(name: str) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
    from gravit_verifier.registry import key_id_for

    KEY_DIR.mkdir(mode=0o700, exist_ok=True)
    path = KEY_DIR / f"{name}.pem"
    if path.exists():
        sys.exit(f"{path} exists; refusing to overwrite a key")
    sk = Ed25519PrivateKey.generate()
    path.write_bytes(sk.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    path.chmod(0o600)
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    pop = f"gravit-operator-v1 {name} {time.strftime('%Y-%m-%d')}"
    print(json.dumps({"private_key_file": str(path), "key_id": key_id_for(pub),
                      "public_key": base64.b64encode(pub).decode(),
                      "proof_of_possession": {"text": pop,
                                              "signature": base64.b64encode(sk.sign(pop.encode())).decode()}}, indent=1))


def sign(name: str, keyfile: str, relation: str) -> None:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key
    from gravit_verifier import EvidenceRecord
    from gravit_verifier.registry import key_id_for, signing_message

    totals = regenerate_and_compare()
    decided = totals["VERIFIED"] + totals["INVALID"] + totals["CONFLICTING"]
    theta = round(totals["VERIFIED"] / decided, 6) if decided else 0.0
    sk = load_pem_private_key(Path(keyfile).read_bytes(), password=None)
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec = EvidenceRecord(record_id=f"{name}:{ts}", theta=theta,
                         result=totals["INVALID"] == 0 and totals["CONFLICTING"] == 0,
                         verifier_id=name, timestamp=ts, provenance=name,
                         integrity_hash=bundle_digest(), key_id=key_id_for(pub))
    rec.signature = base64.b64encode(sk.sign(signing_message(ARTIFACT_ID, rec))).decode()
    out = ROOT / "attestations" / f"{name}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"relation": relation, "totals": totals, "record": rec.__dict__}, indent=1, sort_keys=True) + "\n")
    print(f"signed {out.relative_to(ROOT)}: theta {theta}, result {rec.result}, bundle {rec.integrity_hash[:16]}")


def decide(registry: str, trust_root: str) -> dict:
    from gravit_verifier import EpistemicState, EvidenceRecord, VerificationRequest, VerifierKernel
    from gravit_verifier.loader import load_bindings_from_dir
    from gravit_verifier.registry import TrustRoot, VerifierRegistry

    reg = VerifierRegistry.from_file(registry, TrustRoot.from_file(trust_root))
    kernel = VerifierKernel(reg)
    import os
    for b in load_bindings_from_dir(os.environ.get("GRAVIT_BINDINGS", ROOT / ".deps" / "verifier" / "bindings")):
        kernel.load_binding(b)
    digest = bundle_digest()
    independent, subject = [], []
    for f in sorted((ROOT / "attestations").glob("*.json")):
        a = json.loads(f.read_text())
        rec = EvidenceRecord(**a["record"])
        if rec.integrity_hash != digest:
            continue  # signed a different bundle: not evidence for this one
        (subject if a["relation"] == "subject" else independent).append(rec)
    d = kernel.verify(VerificationRequest(artifact_id=ARTIFACT_ID, claimed_state=EpistemicState.VERIFIED_FACT,
                                          evidence=independent))
    out = {"artifact_id": ARTIFACT_ID, "bundle": digest,
           "independent_attestations": [r.verifier_id for r in independent],
           "subject_acknowledgements": [r.verifier_id for r in subject],
           "decision": d.to_dict()}
    print(json.dumps({k: out[k] for k in ("independent_attestations", "subject_acknowledgements")}),
          d.status.value, d.final_epistemic_state.value if d.final_epistemic_state else None)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("name")
    s = sub.add_parser("sign"); s.add_argument("name"); s.add_argument("keyfile")
    s.add_argument("--relation", choices=["independent", "subject"], required=True)
    dd = sub.add_parser("decide"); dd.add_argument("registry"); dd.add_argument("trust_root")
    a = ap.parse_args()
    if a.cmd == "keygen":
        keygen(a.name)
    elif a.cmd == "sign":
        sign(a.name, a.keyfile, a.relation)
    else:
        decide(a.registry, a.trust_root)
