import base64

import pytest

from app.domain.review import ReviewCaseIntegrityError
from app.infrastructure.content_cipher import (
    ContentCipher,
    ContentKeyUnavailableError,
    SealedContent,
    parse_content_keys,
)

_KEY = base64.b64encode(b"k" * 32).decode()
_OTHER = base64.b64encode(b"o" * 32).decode()


def test_sealed_content_opens_only_under_its_id():
    cipher = parse_content_keys(f"k1:{_KEY}")
    sealed = cipher.seal("case:1", b"synthetic")

    assert cipher.open("case:1", sealed) == b"synthetic"
    assert sealed.key_id == "k1"
    assert len(sealed.nonce) == 12
    assert b"synthetic" not in sealed.ciphertext
    with pytest.raises(ReviewCaseIntegrityError, match="failed authentication"):
        cipher.open("case:2", sealed)


def test_each_seal_uses_a_fresh_nonce():
    cipher = parse_content_keys(f"k1:{_KEY}")

    assert cipher.seal("c", b"x").nonce != cipher.seal("c", b"x").nonce


def test_first_key_seals_and_every_key_opens():
    old = parse_content_keys(f"k1:{_KEY}").seal("c", b"x")
    cipher = parse_content_keys(f"k2:{_OTHER}, k1:{_KEY}")

    assert cipher.seal("c", b"x").key_id == "k2"
    assert cipher.open("c", old) == b"x"


def test_unknown_key_is_reported_without_material():
    cipher = parse_content_keys(f"k1:{_KEY}")

    with pytest.raises(ContentKeyUnavailableError, match="'k9'"):
        cipher.open("c", SealedContent("k9", b"0" * 12, b"x"))


def test_repr_hides_key_material():
    assert repr(parse_content_keys(f"k1:{_KEY}")) == "ContentCipher(active_key_id='k1')"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "unique IDs"),
        (_KEY, "unique IDs"),
        (f"bad id:{_KEY}", "unique IDs"),
        (f"k1:{_KEY},k1:{_OTHER}", "unique IDs"),
        ("k1:not*base64", "'k1' is not base64"),
        (f"k1:{base64.b64encode(b'short').decode()}", "'k1' must be 32 bytes"),
    ],
)
def test_invalid_key_configuration_is_rejected(value, message):
    with pytest.raises(ValueError, match=message) as raised:
        parse_content_keys(value)

    assert _KEY not in str(raised.value)


def test_active_key_must_be_configured():
    with pytest.raises(ValueError, match="active content key"):
        ContentCipher({"k1": b"k" * 32}, "k2")
