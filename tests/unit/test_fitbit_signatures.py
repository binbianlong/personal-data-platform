"""Use real Tink keys while keeping key discovery and Google token transport local."""

import pytest
import tink
from tink import json_proto_keyset_format, signature

from personal_data_platform.sources.fitbit.signatures import GoogleTaskIdentity, TinkSignatures
from personal_data_platform.sources.fitbit.webhook import AuthenticationError


def key_pair():
    signature.register()
    private = tink.new_keyset_handle(signature.signature_key_templates.ECDSA_P256)
    public = json_proto_keyset_format.serialize_without_secret(private.public_keyset_handle())
    return private.primitive(signature.PublicKeySign), public


def test_tink_cache_and_immediate_rotation_refresh_verify_exact_bytes() -> None:
    old, old_keyset = key_pair()
    new, new_keyset = key_pair()
    keysets = [old_keyset, new_keyset]
    calls = []

    def fetch():
        calls.append(True)
        return keysets.pop(0) if keysets else new_keyset

    verifier = TinkSignatures(fetch_keyset=fetch)
    assert verifier.verify(old.sign(b"body"), b"body")
    assert verifier.verify(old.sign(b"body2"), b"body2")
    assert len(calls) == 1
    assert verifier.verify(new.sign(b"body3"), b"body3")
    assert len(calls) == 2
    assert not verifier.verify(new.sign(b"body"), b"changed")


def test_tink_expired_keyset_refreshes_before_verification() -> None:
    signer, public = key_pair()
    calls = []
    now = [0.0]
    verifier = TinkSignatures(
        fetch_keyset=lambda: calls.append(True) or public,
        monotonic=lambda: now[0],
        refresh_seconds=10,
    )
    assert verifier.verify(signer.sign(b"body"), b"body")
    now[0] = 11
    assert verifier.verify(signer.sign(b"body"), b"body")
    assert len(calls) == 2


def test_task_identity_requires_verified_exact_service_account_and_audience() -> None:
    expected = {
        "iss": "https://accounts.google.com",
        "aud": "https://service.run.app",
        "email": "task@project.iam.gserviceaccount.com",
        "email_verified": True,
    }
    observed = []

    def verify(token, *, audience):
        observed.append((token, audience))
        return expected

    identity = GoogleTaskIdentity(
        audience="https://service.run.app",
        service_account="task@project.iam.gserviceaccount.com",
        verify_token=verify,
    )
    identity.authenticate("Bearer signed-id-token")
    assert observed == [("signed-id-token", "https://service.run.app")]
    for claim, bad_value in [
        ("iss", "https://attacker.example"),
        ("aud", "https://other.run.app"),
        ("email", "other@project.iam.gserviceaccount.com"),
        ("email_verified", False),
        ("email_verified", "true"),
    ]:
        previous = expected[claim]
        expected[claim] = bad_value
        with pytest.raises(AuthenticationError):
            identity.authenticate("Bearer signed-id-token")
        expected[claim] = previous
    with pytest.raises(AuthenticationError):
        identity.authenticate(None)


def test_task_token_verification_failure_is_not_authorization() -> None:
    def invalid(token, *, audience):
        raise ValueError("invalid JWT")

    identity = GoogleTaskIdentity(
        audience="https://service.run.app", service_account="task", verify_token=invalid
    )
    with pytest.raises(AuthenticationError):
        identity.authenticate("Bearer invalid")
