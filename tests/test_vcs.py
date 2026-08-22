"""One repo, one branch per strategy (TRD §5.2)."""
from __future__ import annotations

import multiprocessing
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
    module's public surface has no *branch*-delete operation to accidentally
    call. `remove_from_branch` removes one file from `deploy/*` (retirement,
    App-Flow §11.2); no method deletes or rewrites a branch's history."""
    public_methods = {name for name in dir(StrategyRepo) if not name.startswith("_")}
    guarded = public_methods - {"remove_from_branch"}
    assert not any("delete" in name or "remove" in name for name in guarded)
    assert "remove_from_branch" in public_methods  # retire only ever touches deploy/*


def test_commit_file_survives_a_missing_working_tree_file(tmp_path):
    """The first `commit_file` call on a branch has nothing to diff against —
    must not raise trying to read a file that doesn't exist yet."""
    repo = StrategyRepo(tmp_path)
    commit_hash, diff = repo.commit_file("strategy/fresh", "strategies/fresh/strategy.py", "y = 1\n", "first")
    assert commit_hash
    assert diff == ""


def test_merge_produces_a_commit_reachable_from_the_target_branch(tmp_path):
    repo = StrategyRepo(tmp_path)
    repo.commit_file("strategy/a", "strategies/a/strategy.py", "a = 1\n", "a: iteration 1")
    merge_commit = repo.merge("deploy/paper", "strategy/a", "promote a")
    assert merge_commit
    result = subprocess.run(
        ["git", "show", "deploy/paper:strategies/a/strategy.py"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout == "a = 1\n"


def test_merge_two_strategies_into_the_same_deploy_branch_without_conflict(tmp_path):
    """TRD §5.3 — many strategies can merge into one `deploy/*` branch
    because each lives at its own path (`strategies/<uid>/strategy.py`)."""
    repo = StrategyRepo(tmp_path)
    repo.commit_file("strategy/a", "strategies/a/strategy.py", "a = 1\n", "a: iteration 1")
    repo.commit_file("strategy/b", "strategies/b/strategy.py", "b = 1\n", "b: iteration 1")
    repo.merge("deploy/paper", "strategy/a", "promote a")
    repo.merge("deploy/paper", "strategy/b", "promote b")

    subprocess.run(["git", "checkout", "deploy/paper"], cwd=tmp_path, capture_output=True, text=True, check=True)
    tracked = sorted(
        subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout.split()
    )
    assert tracked == ["strategies/a/strategy.py", "strategies/b/strategy.py"]


def test_merge_is_idempotent(tmp_path):
    repo = StrategyRepo(tmp_path)
    repo.commit_file("strategy/a", "strategies/a/strategy.py", "a = 1\n", "a: iteration 1")
    first = repo.merge("deploy/paper", "strategy/a", "promote a")
    second = repo.merge("deploy/paper", "strategy/a", "promote a (retry)")
    assert first == second


def _commit_many(root, n: int, attempts: int) -> None:
    """Module-level (not a closure) so it is picklable for `multiprocessing`
    regardless of start method."""
    repo = StrategyRepo(root)
    for i in range(attempts):
        repo.commit_file(f"strategy/s{n}", f"strategies/s{n}/strategy.py", f"x = {i}\n", f"iteration {i}")


def test_concurrent_workers_do_not_corrupt_the_shared_repo(tmp_path):
    """`Dispatcher.max_concurrent` defaults to 4 (Stage 4) — nothing stops two
    `IMPLEMENT`/`FIX_CODE` jobs for two different strategies landing in two
    worker subprocesses at once, both calling into this same shared repo.

    Without the exclusive lock in `StrategyRepo._locked`, this reproduces
    immediately and destructively: real processes racing `git checkout` /
    `git init` / `git commit` against one shared working tree left most
    strategies either crashed or missing their own file entirely (verified by
    hand while diagnosing this). With the lock, every worker's final commit
    must be intact on its own branch, none of the others', with no crash.
    """
    attempts = 5
    workers = 4
    procs = [
        multiprocessing.Process(target=_commit_many, args=(tmp_path, n, attempts)) for n in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    for n, p in enumerate(procs):
        assert p.exitcode == 0, f"worker {n} crashed (exitcode {p.exitcode})"

    for n in range(workers):
        result = subprocess.run(
            ["git", "show", f"strategy/s{n}:strategies/s{n}/strategy.py"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"strategy/s{n} missing its file: {result.stderr}"
        assert result.stdout == f"x = {attempts - 1}\n"
