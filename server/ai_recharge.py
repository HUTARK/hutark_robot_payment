#!/usr/bin/env python3
"""
ai_recharge.py - Hutark side of the robot AI plan: signed recharge codes.

KEEP hutark_ai_private.key SECRET (server only, never on a robot, never in
git). Robots only get hutark_ai_pubkey.txt.

    pip install cryptography

    # once: make the key pair
    python3 ai_recharge.py keygen
        -> hutark_ai_private.key   (secret)
        -> hutark_ai_pubkey.txt    (copy to every robot's ~/pi_assistant)

    # issue a code by hand (testing / offline payment)
    python3 ai_recharge.py sign --robot LIZA001 --credit 300 [--days 30]

    # check a code
    python3 ai_recharge.py show HTK1.xxxx.yyyy

In your backend:
    from ai_recharge import make_code
    code = make_code(robot_id="LIZA001", credit_inr=300)

Code = "HTK1." + base64url(JSON payload) + "." + base64url(Ed25519 signature
of "HTK1." + base64url(payload)). The payload carries the OpenAI prices and
the USD->INR rate the robot uses to charge each answer, so Hutark controls
pricing (and margin) here, not on the robots.
"""
import argparse, base64, json, os, sys, time, uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.hazmat.primitives import serialization

HERE = os.path.dirname(os.path.abspath(__file__))
PRIVATE_KEY_FILE = os.environ.get("HUTARK_AI_PRIVATE_KEY",
                                  os.path.join(HERE, "hutark_ai_private.key"))
# public key lives next to the private key (default: this folder, as before)
PUBLIC_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(PRIVATE_KEY_FILE)),
                               "hutark_ai_pubkey.txt")
PREFIX = "HTK1."

# ── PRICING (edit here; goes into every new code) ────────────────────────
# USD per 1M tokens: [input, cached input, output]. Check
# https://developers.openai.com/api/docs/pricing before changing models.
PRICES_USD_PER_1M = {
    "gpt-5.4-nano": [0.20, 0.02, 1.25],
    "default":      [1.25, 0.125, 10.00],   # any model not listed
}
INR_PER_USD = 90.0      # set to today's rate (+ any margin you want)


def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def keygen():
    if os.path.exists(PRIVATE_KEY_FILE):
        sys.exit(f"{PRIVATE_KEY_FILE} already exists - not overwriting "
                 "(a new key would invalidate every robot).")
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw,
                            serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    fd = os.open(PRIVATE_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(raw.hex() + "\n")
    pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    with open(PUBLIC_KEY_FILE, "w") as f:
        f.write(pub.hex() + "\n")
    print(f"private key: {PRIVATE_KEY_FILE}  (SECRET - server only)")
    print(f"public key : {PUBLIC_KEY_FILE}  -> copy to each robot")


def _private_key():
    with open(PRIVATE_KEY_FILE) as f:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))


def make_code(robot_id, credit_inr, days=30, recharge_id=None,
              prices=None, inr_per_usd=None):
    """Signed recharge code for one robot. recharge_id must be unique per
    payment (use the payment id) - the robot accepts each id once."""
    payload = {
        "v": 1,
        "robot_id": str(robot_id),
        "recharge_id": str(recharge_id or uuid.uuid4().hex),
        "credit_inr": round(float(credit_inr), 2),
        "days": days,
        "issued_at": int(time.time()),
        "prices": prices or PRICES_USD_PER_1M,
        "inr_per_usd": float(inr_per_usd or INR_PER_USD),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _private_key().sign((PREFIX + body).encode())
    return f"{PREFIX}{body}.{_b64e(sig)}"


def read_code(code, pubkey_hex=None):
    """(valid_signature, payload). For support / tests."""
    _, body, sig = code.strip().split(".")
    pub = bytes.fromhex(pubkey_hex or open(PUBLIC_KEY_FILE).read().strip())
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(_b64d(sig), (PREFIX + body).encode())
        ok = True
    except Exception:
        ok = False
    return ok, json.loads(_b64d(body))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen")
    s = sub.add_parser("sign")
    s.add_argument("--robot", required=True)
    s.add_argument("--credit", type=float, required=True, help="rupees of API usage")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--id", default=None, help="recharge id (default: random)")
    sh = sub.add_parser("show")
    sh.add_argument("code")
    a = ap.parse_args()
    if a.cmd == "keygen":
        keygen()
    elif a.cmd == "sign":
        print(make_code(a.robot, a.credit, a.days, a.id))
    else:
        ok, p = read_code(a.code)
        print("signature:", "VALID" if ok else "INVALID")
        print(json.dumps(p, indent=1))
