"""One repo, one branch per strategy (TRD §5.2)."""
from __future__ import annotations

import subprocess

from aqrl.vcs import StrategyRepo


def test_ensure_branch_creates_a_fresh_orphan_branch(tmp_path):
    repo = StrategyRepo(tmp_path)
    repo.ensure_branch("strategy/a")
    result = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
    assert "init: strategy/a" in result.stdout


def test_ensure_branch_is_idempotent(tmp_path):
    repo = StrategyRepo(tmp_path)
    repo.ensure_branch("strategy/a")
    first_head = repo.head_commit("strategy/a")
    repo.ensure_branch("strategy/a")  # must be a no-op, not re-init
    assert repo.head_commit("strategy/a") == first_head


def test_commit_file_first_commit_has_no_parent_diff(tmp_path):
    repo = StrategyRepo(tmp_path)
    commit_hash, diff = repo.commit_file("strategy/a", "strategies/a/strategy.py", "x = 1\n", "iteration 1")
    assert commit_hash
    assert diff == ""


def test_commit_file_is_idempotent_on_identical_content(tmp_path):
    repo = StrategyRepo(tmp_path)
    c1, _ = repo.commit_file("strategy/a", "strategies/a/strategy.py", "x = 1\n", "iteration 1")
    c2, diff = repo.commit_file("strategy/a", "strategies/a/strategy.py", "x = 1\n", "iteration 1 (retry)")
    assert c1 == c2
    assert diff == ""
    log = subprocess.run(["git", "log", "--oneline", "strategy/a"], cwd=tmp_path, capture_output=True, text=True)
    # Exactly one real commit plus the branch's init commit — the retry must
    # not have added a second, meaningless commit for identical content.
    assert len(log.stdout.strip().splitlines()) == 2


def test_commit_file_second_commit_produces_a_real_diff(tmp_path):
    repo = StrategyRepo(tmp_path)
    repo.commit_file("strategy/a", "strategies/a/strategy.py", "x = 1\n", "iteration 1")
    c2, diff = repo.commit_file("strategy/a", "strategies/a/strategy.py", "x = 2\n", "iteration 2")
    assert "-x = 1" in diff
    assert "+x = 2" in diff
    assert c2


def test_two_strategies_get_isolated_orphan_branches(tmp_path):
    """Two unrelated hypotheses share no content (TRD §5.2) — a file
    committed to one strategy's branch must never appear on another's."""
    repo = StrategyRepo(tmp_path)
    repo.commit_file("strategy/a", "strategies/a/strategy.py", "a = 1\n", "a: iteration 1")
    repo.commit_file("strategy/b", "strategies/b/strategy.py", "b = 1\n", "b: iteration 1")

    subprocess.run(["git", "checkout", "strategy/b"], cwd=tmp_path, capture_output=True, text=True, check=True)
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout.split()
    assert tracked == ["strategies/b/strategy.py"]


def test_branches_are_never_deleted_by_anything_in_this_module():
    """Not a runtime assertion — a documentation-level guard that this
    module's public surface has no delete operation to accidentally call."""
    public_methods = {name for name in dir(StrategyRepo) if not name.startswith("_")}
    assert not any("delete" in name or "remove" in name for name in public_methods)


def test_commit_file_survives_a_missing_working_tree_file(tmp_path):
    """The first `commit_file` call on a branch has nothing to diff against —
    must not raise trying to read a file that doesn't exist yet."""
    repo = StrategyRepo(tmp_path)
    commit_hash, diff = repo.commit_file("strategy/fresh", "strategies/fresh/strategy.py", "y = 1\n", "first")
    assert commit_hash
    assert diff == ""
