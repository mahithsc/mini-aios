"""App-layer crypto for BLE provisioning v2 (see aios-mobile docs/ble-provisioning-v2.md).

Mirrors the phone side (aios-mobile src/services/provisioning/crypto.ts). The BLE
transport is plain; confidentiality/authenticity live here.

- AES-256-GCM for the credential handoff.
- X25519 ECDH + HKDF-SHA256 to derive a one-time session key on first-time claim.

Wire format for an encrypted blob (the CREDS characteristic value):
    base64( nonce[12] || ciphertext || tag[16] )
GCM appends the 16-byte tag to the ciphertext. The per-message `challenge`
(read from the box's CHALLENGE characteristic) is the GCM AAD, binding a message
to that session so it can't be replayed.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HKDF_INFO = b"aios-provisioning-v2"
NONCE_LEN = 12


def seal(key: bytes, plaintext: bytes, challenge: bytes) -> str:
    """Encrypt with AES-256-GCM (AAD=challenge); return the base64 wire blob."""
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, challenge)  # ct includes the tag
    return base64.b64encode(nonce + ct).decode("ascii")


def open_blob(key: bytes, blob: str, challenge: bytes) -> bytes:
    """Inverse of seal(); raises InvalidTag if key/AAD don't verify."""
    raw = base64.b64decode(blob)
    nonce, ct = raw[:NONCE_LEN], raw[NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, challenge)


def seal_credentials(key: bytes, ssid: str, password: str, challenge: bytes) -> str:
    return seal(key, json.dumps({"ssid": ssid, "password": password}).encode(), challenge)


def open_credentials(key: bytes, blob: str, challenge: bytes) -> dict:
    return json.loads(open_blob(key, blob, challenge))


def generate_ephemeral() -> tuple[X25519PrivateKey, bytes]:
    """A fresh X25519 keypair; returns (private_key_obj, raw_public_bytes[32])."""
    priv = X25519PrivateKey.generate()
    return priv, priv.public_key().public_bytes_raw()


def derive_session_key(private_key: X25519PrivateKey, peer_public: bytes) -> bytes:
    """32-byte session key from our private key + the peer's raw public key."""
    shared = private_key.exchange(X25519PublicKey.from_public_bytes(peer_public))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(shared)


def new_provisioning_key() -> bytes:
    """Mint the persistent per-box key established at first claim."""
    return os.urandom(32)
