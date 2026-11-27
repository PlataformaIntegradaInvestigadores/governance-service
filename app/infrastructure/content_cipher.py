import base64
import binascii
import os
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.domain.review import ReviewCaseIntegrityError

_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")


class ContentKeyUnavailableError(Exception):
    """No key is configured to seal or open this content."""


@dataclass(frozen=True)
class SealedContent:
    key_id: str
    nonce: bytes
    ciphertext: bytes


class ContentCipher:
    """AES-256-GCM; the content ID is authenticated, so rows cannot be swapped."""

    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise ValueError("The active content key must be configured")
        self._keys = {key_id: AESGCM(key) for key_id, key in keys.items()}
        self._active = active_key_id

    def __repr__(self) -> str:
        return f"ContentCipher(active_key_id={self._active!r})"

    def seal(self, content_id: str, plaintext: bytes) -> SealedContent:
        nonce = os.urandom(12)
        ciphertext = self._keys[self._active].encrypt(
            nonce, plaintext, self._associated_data(content_id, self._active)
        )
        return SealedContent(self._active, nonce, ciphertext)

    def open(self, content_id: str, sealed: SealedContent) -> bytes:
        key = self._keys.get(sealed.key_id)
        if key is None:
            raise ContentKeyUnavailableError(f"Unknown content key {sealed.key_id!r}")
        try:
            return key.decrypt(
                sealed.nonce,
                sealed.ciphertext,
                self._associated_data(content_id, sealed.key_id),
            )
        except InvalidTag as error:
            raise ReviewCaseIntegrityError(
                "Protected content failed authentication"
            ) from error

    @staticmethod
    def _associated_data(content_id: str, key_id: str) -> bytes:
        return f"governance-content:{key_id}:{content_id}".encode()


def parse_content_keys(value: str) -> ContentCipher:
    """`id:base64key[,id:base64key...]`; the first key seals, all keys open.

    Error messages never include key material.
    """
    keys: dict[str, bytes] = {}
    for entry in value.split(","):
        key_id, separator, encoded = entry.strip().partition(":")
        if not separator or not _KEY_ID.fullmatch(key_id) or key_id in keys:
            raise ValueError("Content keys need unique IDs: id:base64key,...")
        try:
            key = base64.b64decode(encoded, validate=True)
        except binascii.Error as error:
            raise ValueError(f"Content key {key_id!r} is not base64") from error
        if len(key) != 32:
            raise ValueError(f"Content key {key_id!r} must be 32 bytes")
        keys[key_id] = key
    return ContentCipher(keys, next(iter(keys)))
