#!/usr/bin/env python3
"""Refresh, verify, encrypt, and resolve the Threads long-lived access token."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


STATE_PATH = Path(os.environ.get("THREADS_TOKEN_STATE", ".threads-token.enc"))
META_PATH = Path(os.environ.get("THREADS_TOKEN_META", ".threads-token-refresh.json"))
REFRESH_URL = "https://graph.threads.net/refresh_access_token"
PROFILE_URL = "https://graph.threads.net/v1.0/me"
AAD = b"hero-news-threads-token-v1"
PBKDF2_ITERATIONS = 600_000


def require_seed() -> str:
    value = os.environ.get("THREADS_TOKEN_SEED", "").strip()
    if not value:
        raise RuntimeError("THREADS_TOKEN_SEED is required.")
    return value


def derive_key(seed: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        seed.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )


def encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def decode_bytes(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def encrypt_token(token: str, seed: str) -> dict[str, Any]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    ciphertext = AESGCM(derive_key(seed, salt)).encrypt(
        nonce,
        token.encode("utf-8"),
        AAD,
    )
    return {
        "version": 1,
        "kdf": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": encode_bytes(salt),
        "nonce": encode_bytes(nonce),
        "ciphertext": encode_bytes(ciphertext),
    }


def decrypt_token(payload: dict[str, Any], seed: str) -> str:
    if payload.get("version") != 1:
        raise RuntimeError("Unsupported encrypted Threads token version.")
    if payload.get("iterations") != PBKDF2_ITERATIONS:
        raise RuntimeError("Unexpected Threads token key-derivation settings.")

    plaintext = AESGCM(
        derive_key(seed, decode_bytes(str(payload["salt"])))
    ).decrypt(
        decode_bytes(str(payload["nonce"])),
        decode_bytes(str(payload["ciphertext"])),
        AAD,
    )
    return plaintext.decode("utf-8")


def current_token(seed: str) -> str:
    if not STATE_PATH.exists():
        return seed

    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        token = decrypt_token(payload, seed).strip()
    except Exception as exc:
        raise RuntimeError("Unable to decrypt the stored Threads token.") from exc

    if not token:
        raise RuntimeError("The stored Threads token is empty.")
    return token


def atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def refresh_token() -> None:
    seed = require_seed()
    old_token = current_token(seed)

    response = requests.get(
        REFRESH_URL,
        params={
            "grant_type": "th_refresh_token",
            "access_token": old_token,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Threads token refresh failed with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    payload = response.json()
    new_token = str(payload.get("access_token", "")).strip()
    expires_in = int(payload.get("expires_in", 0))
    if not new_token:
        raise RuntimeError("Threads token refresh returned no access_token.")
    if expires_in < 86_400:
        raise RuntimeError(
            f"Threads token refresh returned an invalid expiry: {expires_in}."
        )

    profile_response = requests.get(
        PROFILE_URL,
        params={
            "fields": "id,username",
            "access_token": new_token,
        },
        timeout=30,
    )
    if not profile_response.ok:
        raise RuntimeError(
            f"Refreshed Threads token verification failed with HTTP "
            f"{profile_response.status_code}: {profile_response.text[:500]}"
        )

    profile = profile_response.json()
    actual_user_id = str(profile.get("id", "")).strip()
    expected_user_id = os.environ.get("THREADS_USER_ID", "").strip()
    if not actual_user_id:
        raise RuntimeError("Threads profile verification returned no user id.")
    if expected_user_id and actual_user_id != expected_user_id:
        raise RuntimeError(
            "Refreshed Threads token belongs to a different user id."
        )

    now = datetime.now(timezone.utc)
    atomic_json_write(STATE_PATH, encrypt_token(new_token, seed))
    atomic_json_write(
        META_PATH,
        {
            "version": 1,
            "refreshed_at": now.isoformat(),
            "expires_in": expires_in,
            "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
            "user_id": actual_user_id,
            "username": str(profile.get("username", "")),
            "same_as_previous": new_token == old_token,
        },
    )

    print(
        "Threads token refreshed and verified: "
        f"user_id={actual_user_id} expires_in={expires_in} "
        f"same_as_previous={str(new_token == old_token).lower()}"
    )


def resolve_token() -> None:
    print(current_token(require_seed()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("refresh", "resolve"))
    args = parser.parse_args()

    try:
        if args.command == "refresh":
            refresh_token()
        else:
            resolve_token()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
