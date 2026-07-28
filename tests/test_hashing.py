"""Content hashing must be stable across processes, machines and runs.

`market_profile_hash`, `cost_model_hash`, `wf_config_hash`, `raw_content_hash`
and `spec_hash` all come from here. Two hashers with subtly different float
formatting would silently partition the result history — precisely the rot TRD
§6.6 exists to prevent.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from aqrl.hashing import canonical_json, content_hash, hash_file, hash_files


def test_key_order_does_not_change_the_hash():
    assert content_hash({"b": 1, "a": 2}) == content_hash({"a": 2, "b": 1})


def test_nested_key_order_does_not_change_the_hash():
    left = {"outer": {"z": [1, {"q": 1, "p": 2}], "a": True}}
    right = {"outer": {"a": True, "z": [1, {"p": 2, "q": 1}]}}
    assert content_hash(left) == content_hash(right)


def test_sequence_order_does_change_the_hash():
    # Order is meaningful in a list; conflating [1,2] with [2,1] would let two
    # different operator DAGs share a spec_hash.
    assert content_hash([1, 2]) != content_hash([2, 1])


def test_sets_are_order_independent():
    assert content_hash({"s": {3, 1, 2}}) == content_hash({"s": {2, 3, 1}})


def test_floats_round_trip_exactly():
    value = 0.1 + 0.2  # 0.30000000000000004
    assert content_hash({"x": value}) == content_hash({"x": 0.30000000000000004})
    assert content_hash({"x": value}) != content_hash({"x": 0.3})


def test_int_and_float_are_distinct():
    assert content_hash({"x": 1}) != content_hash({"x": 1.0})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(bad):
    # NaN never equals itself; hashing it produces an identifier whose meaning
    # depends on who reads it.
    with pytest.raises(ValueError, match="non-finite"):
        content_hash({"x": bad})


def test_dates_decimals_and_paths_are_supported():
    payload = {"d": date(2020, 1, 1), "n": Decimal("1.50"), "p": Path("a/b.parquet")}
    assert content_hash(payload) == content_hash(payload)
    assert "2020-01-01" in canonical_json(payload)


def test_unsupported_types_raise_rather_than_stringify():
    class Opaque:
        pass

    with pytest.raises(TypeError):
        content_hash({"x": Opaque()})


def test_canonical_json_is_compact_and_sorted():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_hash_files_is_independent_of_traversal_order(tmp_path: Path):
    root = tmp_path / "snap"
    (root / "sub").mkdir(parents=True)
    (root / "a.parquet").write_bytes(b"alpha")
    (root / "sub" / "b.parquet").write_bytes(b"beta")
    files = [root / "a.parquet", root / "sub" / "b.parquet"]
    assert hash_files(files, root) == hash_files(list(reversed(files)), root)


def test_hash_files_changes_when_content_changes(tmp_path: Path):
    root = tmp_path / "snap"
    root.mkdir()
    path = root / "a.parquet"
    path.write_bytes(b"alpha")
    before = hash_files([path], root)
    path.write_bytes(b"alphb")
    assert hash_files([path], root) != before


def test_hash_files_is_independent_of_mount_point(tmp_path: Path):
    """The same bytes in the same layout hash identically wherever mounted."""
    hashes = []
    for name in ("mount_a", "mount_b"):
        root = tmp_path / name
        root.mkdir()
        (root / "a.parquet").write_bytes(b"alpha")
        hashes.append(hash_files([root / "a.parquet"], root))
    assert hashes[0] == hashes[1]


def test_hash_file_matches_known_sha256(tmp_path: Path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"")
    assert hash_file(path) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_hash_is_stable_across_runs():
    # Hardcoded so a change in the canonicalisation rules fails loudly here
    # rather than silently orphaning every stored provenance hash.
    assert content_hash({"scheme": "rolling", "test_years": 1, "train_years": [1, 2, 3]}) == (
        "801bd3667c1c57e49b7be5c3923a5832fdf81c9d6aa49e6bf0aed0967c8be263"
    )
