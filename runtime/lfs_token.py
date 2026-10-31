"""Scoped, expiring bearer tokens for SSH-authenticated Git LFS transport."""

import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time


MAX_TOKEN = 4096
MAX_TTL = 3600


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode(value):
    if not isinstance(value, str) or not value or len(value) > MAX_TOKEN:
        raise ValueError("Invalid LFS token encoding")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def token_path(root):
    return Path(root) / "lfs-token.key"


def ensure_secret(root):
    root = Path(root)
    path = token_path(root)
    if path.exists():
        secret = path.read_bytes()
        if len(secret) != 32:
            raise ValueError("LFS token key must contain exactly 32 bytes")
    else:
        if not (root / "maintenance").is_file():
            raise ValueError("Enter maintenance before creating the LFS token key")
        temporary = root / f".lfs-token-{os.getpid()}"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, secrets.token_bytes(32))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        secret = path.read_bytes()
    if len(secret) != 32:
        raise ValueError("LFS token key must contain exactly 32 bytes")
    os.chmod(path, 0o600)
    return secret


def issue(cfg, username, repo, operation, generation, ttl=300):
    if operation not in ("download", "upload"):
        raise ValueError("Invalid LFS token operation")
    if not isinstance(ttl, int) or not 30 <= ttl <= MAX_TTL:
        raise ValueError("Invalid LFS token lifetime")
    now = int(time.time())
    payload = {
        "aud": "git-lfs",
        "exp": now + ttl,
        "generation": generation,
        "iat": now,
        "op": operation,
        "repo": repo,
        "user": username,
    }
    body = canonical(payload)
    secret = ensure_secret(cfg["access"]["root"])
    signature = hmac.new(secret, b"v1." + body, hashlib.sha256).digest()
    return f"v1.{encode(body)}.{encode(signature)}", payload


def verify(cfg, token, repo, operation, generation):
    try:
        if not isinstance(token, str) or len(token) > MAX_TOKEN:
            raise ValueError("Invalid LFS token size")
        version, body, signature = token.split(".")
        if version != "v1":
            raise ValueError("Unsupported LFS token version")
        raw_body = decode(body)
        supplied = decode(signature)
        secret = ensure_secret(cfg["access"]["root"])
        expected = hmac.new(secret, b"v1." + raw_body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("Invalid LFS token signature")
        payload = json.loads(raw_body)
        now = int(time.time())
        if (not isinstance(payload, dict)
                or payload.get("aud") != "git-lfs"
                or payload.get("repo") != repo
                or payload.get("op") != operation
                or payload.get("generation") != generation
                or type(payload.get("iat")) is not int
                or type(payload.get("exp")) is not int
                or payload["iat"] > now + 60
                or not payload["iat"] <= now < payload["exp"]
                or payload["exp"] - payload["iat"] > MAX_TTL
                or not isinstance(payload.get("user"), str)):
            raise ValueError("LFS token scope or lifetime is invalid")
        return payload["user"]
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("LFS token rejected") from exc
