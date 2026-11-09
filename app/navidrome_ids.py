"""Navidrome 0.64 canonical IDs.

Navidrome 0.64.0 re-encoded every internal ID into one canonical form: a
22-character, zero-padded base62 rendering of a 128-bit value. The rewrite is
deterministic, so an ID stored before the upgrade can be converted without
asking the server. This is a port of ``canonicalID()`` from Navidrome's
``db/migrations/20260720015443_uniform_canonical_ids.go``.
"""
import hashlib

# Go's big.Int.Text(62) digit order
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
_HEX = set("0123456789abcdefABCDEF")

# First Navidrome release that serves canonical IDs
CANONICAL_SINCE = (0, 64, 0)


def _encode(value: int) -> str:
    digits = []
    while value:
        value, r = divmod(value, 62)
        digits.append(_ALPHABET[r])
    return "".join(reversed(digits)).rjust(22, "0")


def _decode62(s: str) -> int | None:
    value = 0
    for c in s:
        i = _INDEX.get(c)
        if i is None:
            return None
        value = value * 62 + i
    return value


def _is_hex(s: str) -> bool:
    return all(c in _HEX for c in s)


def canonical_id(s: str | None) -> str | None:
    """Map any historical Navidrome ID shape to its 0.64+ canonical form.

    - 32-char hex: same 128-bit value, re-encoded as base62
    - 22-char base62: kept if it fits in 128 bits, otherwise md5 of it, re-encoded
    - 36-char UUID: dashes stripped, then as hex
    - anything else (empty, share IDs, unknown shapes): unchanged
    """
    if not s:
        return s
    if len(s) == 22:
        # big.Int.SetString accepts one leading sign; a negative value is kept
        if s[0] == "-":
            return s
        value = _decode62(s[1:] if s[0] == "+" else s)
        if value is None or value.bit_length() <= 128:
            return s
        return _encode(int.from_bytes(hashlib.md5(s.encode()).digest(), "big"))
    if len(s) == 32:
        return _encode(int(s, 16)) if _is_hex(s) else s
    if len(s) == 36:
        if s[8] != "-" or s[13] != "-" or s[18] != "-" or s[23] != "-":
            return s
        h = s[:8] + s[9:13] + s[14:18] + s[19:23] + s[24:]
        return _encode(int(h, 16)) if _is_hex(h) else s
    return s


def parse_version(v: str | None) -> tuple[int, ...] | None:
    """'0.64.2 (10114574)' -> (0, 64, 2). None if it doesn't look like a version."""
    if not v:
        return None
    head = v.strip().split()[0].lstrip("v")
    parts = []
    for p in head.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def serves_canonical_ids(server_type: str | None, server_version: str | None) -> bool:
    """True when the server is Navidrome 0.64.0 or newer."""
    if (server_type or "").lower() != "navidrome":
        return False
    v = parse_version(server_version)
    return bool(v) and v >= CANONICAL_SINCE
