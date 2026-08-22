"""One repo, one branch per strategy (TRD §5.2) — the git half of storage.

**SQLite holds metadata; git holds code** (TRD §5.1). This module is the only
thing in the codebase that shells out to `git`, and it exists to make three
guarantees TRD §5.2 states as requirements, not preferences:

* **A strategy's code lives on `strategy/<strategy_uid>`, created on its first
  `IMPLEMENT` job.** `ensure_branch` is idempotent — calling it again for a
  branch that already exists is a no-op, which matters because a crashed
  worker's retried job must not fail on "branch already exists".
* **Branches are never deleted**, including for rejected strategies — that is
  a policy this module simply never provides an operation for.
* **Each strategy lives at its own path** (`strategies/<uid>/strategy.py`), so
  many strategies can later merge into one `deploy/*` branch (TRD §5.3)
  without touching the same file. Enforced by the caller (`render.py`), not
  here — this module writes whatever path it is given.

**One repo, used from possibly many worker processes — genuinely, not just in
theory.** `Dispatcher.max_concurrent` (Stage 4) defaults to 4, and nothing
stops two `IMPLEMENT`/`FIX_CODE` jobs for two *different* strategies from
running in two worker subprocesses at once. Every public method here
therefore holds an OS file lock (`fcntl.flock` on POSIX, `msvcrt.locking` on
Windows, exclusive, blocking) across
its *entire* git sequence — `checkout` changes which branch the one shared
working tree points at, so two processes interleaving a checkout with
another's add/commit is not a slow-down, it is corruption: writes landing on
the wrong branch, or `git init`/`checkout --orphan` racing entirely. Verified
by literally reproducing it — four concurrent processes each committing to
their own branch, unlocked, left three of the four either crashed or with
their strategy's file simply absent from the branch it should have been on.
The lock makes concurrent callers correct by serialising them, not by
avoiding contention; git itself is fast enough per call that this is not a
throughput concern at the scale one research lab's worker pool runs at.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if os.name == "nt":
    import msvcrt
else:
    import fcntl

if TYPE_CHECKING:
    from io import TextIOWrapper

__all__ = ["StrategyRepo", "VcsError"]

_LOCK_FILENAME = ".aqrl-vcs.lock"


def _lock_file(handle: TextIOWrapper) -> None:
    """Exclusive, blocking lock on `handle` (POSIX: `flock`; Windows: `msvcrt`).

    `msvcrt.LK_LOCK` gives up after ~10 seconds of contention (`OSError
    EDEADLOCK`) where `flock` blocks indefinitely — the retry loop restores
    `flock`'s blocking semantics so a slow `git` sequence on another worker
    cannot fail this one.
    """
    if os.name == "nt":
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX)


def _unlock_file(handle: TextIOWrapper) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)

_LOCK_FILENAME = ".aqrl-vcs.lock"


class VcsError(RuntimeError):
    """A `git` invocation failed. Always carries the command and stderr."""


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise VcsError(f"git {' '.join(args)!r} failed in {cwd}: {result.stderr.strip()}")
    return result.stdout.strip()


class StrategyRepo:
    """The one repo every strategy branch lives in.

    Initialised lazily on first use — a fresh checkout of the AQRL codebase
    has no strategy history yet, and creating it on demand means there is
    nothing to provision before Stage 5 runs for the first time.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- cross-process locking --------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Exclusive, blocking, process-wide — the one thing that makes every
        public method below safe to call concurrently across worker
        subprocesses sharing this same `root` (see module docstring).

        A fresh `open()` per acquisition rather than a cached handle: `flock`
        locks belong to the *open file description*, not the path or the
        process, so two calls in the *same* process each get their own
        description and genuinely block each other — which is exactly what
        prevents the public methods below from ever nesting two acquisitions
        (each calls a private `_unlocked` helper internally, never itself).
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / _LOCK_FILENAME).open("w") as handle:
            _lock_file(handle)
            try:
                yield
            finally:
                _unlock_file(handle)

    # -- public API ---------------------------------------------------------------

    def ensure_repo(self) -> None:
        with self._locked():
            self._ensure_repo_unlocked()

    def ensure_branch(self, branch: str) -> None:
        """Create `branch` if it does not exist yet. Idempotent (TRD §5.2)."""
        with self._locked():
            self._ensure_branch_unlocked(branch)

    def commit_file(
        self, branch: str, relative_path: str, content: str, message: str
    ) -> tuple[str, str]:
        """Write `content` to `relative_path` on `branch` and commit it.

        Returns `(commit_hash, diff_from_parent)`. `diff_from_parent` is empty
        on the branch's first real commit — there is no parent to diff
        against (Backend-Schema §5: *"empty on iteration 1"*).

        Content-addressed and therefore idempotent: committing byte-identical
        content twice produces `git commit --allow-empty` only if nothing
        changed since the parent, which `git commit` itself already treats as
        a no-op error we swallow by checking `git status` first — a crash
        between `commit_file` and the caller's database write is recovered by
        the retried job re-deriving the identical commit rather than adding a
        duplicate.
        """
        with self._locked():
            self._ensure_branch_unlocked(branch)
            _run(["checkout", branch], self.root)

            target = self.root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            previous = target.read_text(encoding="utf-8") if target.exists() else None
            target.write_text(content, encoding="utf-8")

            _run(["add", relative_path], self.root)
            status = _run(["status", "--porcelain", "--", relative_path], self.root)

            parent_hash = self._head_commit_unlocked(branch)
            diff = "" if previous is None else self._diff(target, previous, content)

            if not status:
                # Nothing changed — the working tree already matched `content`
                # (a retried job re-rendering the same spec). The existing
                # commit is the correct answer; report it rather than
                # creating a meaningless empty commit.
                return parent_hash, diff

            _run(["commit", "-m", message], self.root)
            return self._head_commit_unlocked(branch), diff

    def head_commit(self, branch: str) -> str:
        with self._locked():
            return self._head_commit_unlocked(branch)

    def merge(self, into: str, from_branch: str, message: str) -> str:
        """Merge `from_branch` into `into`, creating `into` if it does not
        exist yet, and return the resulting commit (TRD §5.3).

        `--allow-unrelated-histories` is mandatory: `_ensure_branch_unlocked`
        creates every strategy branch as an orphan, so `strategy/<uid>` and
        `deploy/paper` share no history by construction (TRD §5.2 — "two
        unrelated hypotheses have no shared content to combine"). Conflicts
        cannot occur because each strategy's file lives at its own path,
        `strategies/<uid>/strategy.py` (`render.py`), the property TRD §5.2
        exists to guarantee.

        Idempotent by git's own semantics: re-merging an already-merged
        branch is a no-op ("Already up to date") and simply returns the
        current HEAD — what makes a crash between this call and the
        caller's database write safe to recover by re-running the approval.
        """
        with self._locked():
            self._ensure_branch_unlocked(into)
            _run(["checkout", into], self.root)
            _run(["merge", "--allow-unrelated-histories", "--no-ff", "-m", message, from_branch], self.root)
            return self._head_commit_unlocked(into)

    def remove_from_branch(self, branch: str, relative_path: str, message: str) -> str:
        """Remove `relative_path` from `branch` and commit — retirement's "leave
        the deploy branch, keep the research branch" step (App-Flow §11.2).
        Never touches `strategy/<uid>`; only ever called against `deploy/paper`
        or `deploy/live`.

        Idempotent: if the file is already gone from the branch (a retried
        retirement), this is a no-op that returns the current HEAD rather
        than failing on `git rm` finding nothing to remove — the same
        crash-safety `merge` provides.
        """
        with self._locked():
            _run(["checkout", branch], self.root)
            if not (self.root / relative_path).exists():
                return self._head_commit_unlocked(branch)
            _run(["rm", relative_path], self.root)
            _run(["commit", "-m", message], self.root)
            return self._head_commit_unlocked(branch)

    # -- unlocked internals — never call these without holding `_locked()` ------

    def _ensure_repo_unlocked(self) -> None:
        if (self.root / ".git").exists():
            return
        _run(["init"], self.root)
        _run(["config", "user.name", "aqrl-agent"], self.root)
        _run(["config", "user.email", "agent@aqrl.local"], self.root)
        # An empty repo has no branch to check out yet; the initial commit
        # (below, on a strategy's first IMPLEMENT) creates one.

    def _branch_exists(self, branch: str) -> bool:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.root,
            capture_output=True,
        )
        return result.returncode == 0

    def _ensure_branch_unlocked(self, branch: str) -> None:
        """Always an orphan branch, whether or not the repo already holds
        other strategies' history: *"two unrelated hypotheses have no shared
        content to combine"* (TRD §5.2). `git checkout --orphan` carries the
        current branch's working-tree files forward as staged adds, so every
        branch but the very first (created on an empty, unborn repo) needs
        its tree wiped back to nothing before the initial commit.
        """
        self._ensure_repo_unlocked()
        if self._branch_exists(branch):
            return
        _run(["checkout", "--orphan", branch], self.root)
        tracked = _run(["ls-files"], self.root)
        if tracked:
            _run(["rm", "-rf", "--cached", "."], self.root)
        for entry in self.root.iterdir():
            if entry.name in (".git", _LOCK_FILENAME):
                continue
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        _run(["commit", "--allow-empty", "-m", f"init: {branch}"], self.root)

    def _head_commit_unlocked(self, branch: str) -> str:
        return _run(["rev-parse", branch], self.root)

    def _diff(self, target: Path, previous: str, content: str) -> str:
        if previous == content:
            return ""
        import difflib

        return "".join(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{target.name}",
                tofile=f"b/{target.name}",
            )
        )
