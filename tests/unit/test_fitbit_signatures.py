import tink
from tink import json_proto_keyset_format, signature

from personal_data_platform.sources.fitbit.signatures import TinkSignatures


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
