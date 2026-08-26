"""Passwords, Keychain, and file encryption — all local, all real.

`generate_password` uses `secrets`, never the model — the SYSTEM prompt is
explicit that text a model produces "is NOT random and NOT cryptographically
secure, even when it looks scrambled" (see agent/loop.py's "THINGS YOU CANNOT
DO IN YOUR HEAD"). This tool is the actual implementation of that rule for
passwords specifically.

Keychain access goes through the `security` CLI, which is what every local
credential manager on macOS ultimately calls. Writing a new item still
prompts the user for Keychain access via the OS's own dialog — Wisp cannot
and does not bypass that.
"""
from __future__ import annotations

import secrets
import string
import subprocess

from service.tools.registry import register

_TIMEOUT = 20


@register(
    "generate_password",
    "Generate a cryptographically random password. Never write a password "
    "yourself — text a language model produces is not random and not secure, "
    "even when it looks scrambled. This uses the OS's real randomness source.",
    {
        "type": "object",
        "properties": {
            "length": {"type": "integer", "description": "Password length. Default 20."},
            "symbols": {"type": "boolean", "description": "Include symbols. Default true."},
        },
    },
    category="assistant_read",
    aliases=["generate a secure password", "give me a random password",
             "make a strong password for this account", "I need a new password"],
)
def generate_password(length: int = 20, symbols: bool = True) -> str:
    try:
        n = max(8, min(128, int(length)))
    except (TypeError, ValueError):
        n = 20
    alphabet = string.ascii_letters + string.digits
    if symbols:
        alphabet += "!@#$%^&*()-_=+[]{}"
    pw = "".join(secrets.choice(alphabet) for _ in range(n))
    return pw


@register(
    "keychain_store",
    "Save a password/secret to macOS Keychain under a name, so it's stored "
    "securely rather than in a note or a chat message. The OS may prompt the "
    "user to allow access the first time.",
    {"type": "object",
     "properties": {
         "service": {"type": "string", "description": "What this credential is for, e.g. 'My Bank Login'."},
         "account": {"type": "string", "description": "Username/account this credential belongs to."},
         "secret": {"type": "string", "description": "The password/secret to store."},
     },
     "required": ["service", "account", "secret"]},
    category="assistant_write",
    aliases=["save this password to my keychain", "store this credential securely",
             "put this password in the keychain safely"],
)
def keychain_store(service: str, account: str, secret: str) -> str:
    s, a, sec = service.strip(), account.strip(), secret
    if not s or not a or not sec:
        return "(error: keychain_store needs `service`, `account`, and `secret`.)"
    # -U updates in place if an item with this service+account already
    # exists, rather than erroring — repeated saves for the same credential
    # (a password rotation) should just work.
    p = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", s, "-a", a, "-w", sec],
        capture_output=True, text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        return f"(could not save to Keychain: {(p.stderr or '').strip()})"
    return f"Saved to Keychain under {s!r} ({a})."


@register(
    "keychain_read",
    "Retrieve a secret previously saved to macOS Keychain with "
    "keychain_store. The OS will prompt the user to allow access.",
    {"type": "object",
     "properties": {
         "service": {"type": "string", "description": "The service name it was saved under."},
         "account": {"type": "string", "description": "The account it was saved under."},
     },
     "required": ["service", "account"]},
    category="assistant_read",
    aliases=["get my bank password from keychain", "look up this saved credential",
             "what password did I save for this"],
)
def keychain_read(service: str, account: str) -> str:
    s, a = service.strip(), account.strip()
    if not s or not a:
        return "(error: keychain_read needs both `service` and `account`.)"
    p = subprocess.run(
        ["security", "find-generic-password", "-s", s, "-a", a, "-w"],
        capture_output=True, text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        if "could not be found" in err.lower() or p.returncode == 44:
            return f"No Keychain item found for {s!r} / {a!r}."
        return f"(could not read Keychain: {err})"
    return p.stdout.strip()


@register(
    "encrypt_file",
    "Password-encrypt (or decrypt) a file using AES-256, via the built-in "
    "openssl. The password is never stored by Wisp — if it's lost, the file "
    "is unrecoverable.",
    {"type": "object",
     "properties": {
         "path": {"type": "string", "description": "File to encrypt/decrypt."},
         "password": {"type": "string", "description": "Password to use."},
         "decrypt": {"type": "boolean", "description": "Decrypt instead of encrypt. Default false."},
     },
     "required": ["path", "password"]},
    category="fs_write",
    aliases=["encrypt this file with a password", "password protect this document",
             "decrypt this file"],
)
def encrypt_file(path: str, password: str, decrypt: bool = False) -> str:
    from pathlib import Path

    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such file: {p})"
    if not password:
        return "(error: encrypt_file needs a `password`.)"

    out = p.with_suffix(p.suffix + (".dec" if decrypt else ".enc"))
    if out.exists():
        return f"(refusing to overwrite {out} — it already exists.)"

    argv = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2",
            "-d" if decrypt else "", "-in", str(p), "-out", str(out),
            "-pass", f"pass:{password}"]
    argv = [a for a in argv if a]  # drop the empty "-d" when encrypting
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        out.unlink(missing_ok=True)
        wrong_pw = "bad decrypt" in (result.stderr or "").lower()
        return (f"(wrong password — could not decrypt {p.name}.)" if wrong_pw and decrypt
                else f"(could not {'decrypt' if decrypt else 'encrypt'} {p.name}: "
                     f"{(result.stderr or '').strip()})")
    return f"{'Decrypted' if decrypt else 'Encrypted'} to {out.name}."
