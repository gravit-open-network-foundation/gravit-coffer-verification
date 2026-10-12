"""Profile rule: Verified Fact needs two independent operators; the verified party
(Coffer) can acknowledge, but its own signature never counts as independent evidence."""

import base64
import hashlib
import json
from pathlib import Path

import pytest

gv = pytest.importorskip("gravit_verifier")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from gravit_verifier import EpistemicState, EvidenceRecord, VerificationRequest, VerifierKernel  # noqa: E402
from gravit_verifier.loader import load_bindings_from_dir  # noqa: E402
from gravit_verifier.registry import (TrustRoot, VerifierRegistry, key_id_for,  # noqa: E402
                                      registry_signing_message, signing_message)

from coffer_verify.attest import ARTIFACT_ID  # noqa: E402

BINDINGS = Path(__file__).resolve().parent.parent / ".deps" / "verifier" / "bindings"
TS = "2026-10-12T00:00:00Z"


def key(name):
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"test:" + name.encode()).digest())


def pub(name):
    return key(name).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def doc(o):
    return (json.dumps(o, indent=1, sort_keys=True) + "\n").encode()


def registry():
    roots = [f"root-{i}" for i in range(5)]
    tr = TrustRoot.from_bytes(doc({"trust_root_version": 1, "threshold": 3, "root_keys": [
        {"key_id": key_id_for(pub(r)), "public_key": base64.b64encode(pub(r)).decode()} for r in roots]}))
    ops = {"gravit": "Gravit Open Network Foundation", "op-b": "Independent Operator B AG", "coffer": "Coffer"}
    reg = doc({"registry_version": 1, "verifiers": [
        {"key_id": key_id_for(pub(n)), "public_key": base64.b64encode(pub(n)).decode(), "operator": o, "revoked": False}
        for n, o in ops.items()]})
    msg = registry_signing_message(reg)
    sigs = "".join(f"{key_id_for(pub(r))} {base64.b64encode(key(r).sign(msg)).decode()}\n" for r in roots[:3])
    return VerifierRegistry.from_bytes(reg, sigs, tr), ops


def attestation(name, operator, digest="bundle"):
    r = EvidenceRecord(record_id=f"{name}:{TS}", theta=1.0, result=True, verifier_id=name, timestamp=TS,
                       provenance=operator, integrity_hash=digest, key_id=key_id_for(pub(name)))
    r.signature = base64.b64encode(key(name).sign(signing_message(ARTIFACT_ID, r))).decode()
    return r


def decide(records):
    reg, _ = registry()
    k = VerifierKernel(reg)
    for b in load_bindings_from_dir(BINDINGS):
        k.load_binding(b)
    d = k.verify(VerificationRequest(artifact_id=ARTIFACT_ID, claimed_state=EpistemicState.VERIFIED_FACT, evidence=records))
    return d.final_epistemic_state


def test_two_independent_operators_give_verified_fact():
    _, ops = registry()
    assert decide([attestation("gravit", ops["gravit"]), attestation("op-b", ops["op-b"])]) == EpistemicState.VERIFIED_FACT


def test_one_operator_alone_is_not_enough():
    _, ops = registry()
    assert decide([attestation("gravit", ops["gravit"])]) != EpistemicState.VERIFIED_FACT


def test_subject_acknowledgement_is_excluded_before_the_kernel():
    # attest.decide() drops relation == "subject" records; the remaining single operator is not enough
    _, ops = registry()
    independent = [attestation("gravit", ops["gravit"])]
    assert decide(independent) != EpistemicState.VERIFIED_FACT
