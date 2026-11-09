import pytest

from app.navidrome_ids import canonical_id, parse_version, serves_canonical_ids

# the exact table from Navidrome's db/migrations/id_canonical_test.go (v0.64.2)
NAVIDROME_VECTORS = [
    ("5cLJPkLA5DK2BADhoeotPk", "5cLJPkLA5DK2BADhoeotPk"),   # fits 128 bits: kept
    ("zzzzzzzzzzzzzzzzzzzzzz", "3LyqmwQBm5IRqlVjNYASwb"),   # overflows: md5 remap
    ("e3b7fc2ae9447bbec37a13bf916e3cf6", "6VHl3uR4kss6sUPKA8Cwnk"),  # legacy hex
    ("f47ac10b-58cc-4372-a567-0e02b2c3d479", "7rke2SAWaicSeSYzkhww6R"),  # uuid
    ("", ""),
    ("aB3xY9kQz1", "aB3xY9kQz1"),                           # share id
    ("0123456789abcdef", "0123456789abcdef"),               # truncated id
    ("!!!!!!!!!!!!!!!!!!!!!!", "!!!!!!!!!!!!!!!!!!!!!!"),   # 22 non-base62
    ("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"),  # 32 non-hex
    ("000000000000000000000000000000000000", "000000000000000000000000000000000000"),  # 36, no dashes
]


@pytest.mark.parametrize("old,new", NAVIDROME_VECTORS)
def test_matches_navidrome(old, new):
    assert canonical_id(old) == new


@pytest.mark.parametrize("old", [v[0] for v in NAVIDROME_VECTORS])
def test_idempotent(old):
    once = canonical_id(old)
    assert canonical_id(once) == once


def test_none_passes_through():
    assert canonical_id(None) is None


def test_uppercase_hex_is_hex():
    assert canonical_id("E3B7FC2AE9447BBEC37A13BF916E3CF6") == "6VHl3uR4kss6sUPKA8Cwnk"


def test_signed_22():
    assert canonical_id("-" + "z" * 21) == "-" + "z" * 21   # negative: kept


@pytest.mark.parametrize("v,want", [
    ("0.64.2 (10114574)", (0, 64, 2)), ("0.63.2 (be10f89c)", (0, 63, 2)),
    ("v0.64.0", (0, 64, 0)), ("1.0", (1, 0)), ("", None), (None, None), ("dev", None)])
def test_parse_version(v, want):
    assert parse_version(v) == want


@pytest.mark.parametrize("t,v,want", [
    ("navidrome", "0.64.0 (x)", True), ("navidrome", "0.65.1", True),
    ("Navidrome", "1.0.0", True), ("navidrome", "0.63.2 (be10f89c)", False),
    ("gonic", "0.64.0", False), (None, "0.64.0", False), ("navidrome", None, False)])
def test_serves_canonical(t, v, want):
    assert serves_canonical_ids(t, v) is want
