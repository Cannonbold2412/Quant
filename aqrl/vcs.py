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

**One repo, used from possibly many worker processes.** Every write here is a
single `git` invocation with no long-lived index lock held across Python code,
so two workers committing to two different strategy branches back-to-back
never contend for more than the instant one `git commit` takes.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

__all__ = ["StrategyRepo", "VcsError"]


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

    def ensure_repo(self) -> None:
        if (self.root / ".git").exists():
            return
        self.root.mkdir(parents=True, exist_ok=True)
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

    def ensure_branch(self, branch: str) -> None:
        """Create `branch` if it does not exist yet. Idempotent (TRD §5.2).

        Always an orphan branch, whether or not the repo already holds other
        strategies' history: *"two unrelated hypotheses have no shared
        content to combine"* (TRD §5.2). `git checkout --orphan` carries the
        current branch's working-tree files forward as staged adds, so every
        branch but the very first (created on an empty, unborn repo) needs its
        tree wiped back to nothing before the initial commit.
        """
        self.ensure_repo()
        if self._branch_exists(branch):
            return
        _run(["checkout", "--orphan", branch], self.root)
        tracked = _run(["ls-files"], self.root)
        if tracked:
            _run(["rm", "-rf", "--cached", "."], self.root)
        for entry in self.root.iterdir():
            if entry.name == ".git":
                continue
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        _run(["commit", "--allow-empty", "-m", f"init: {branch}"], self.root)

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
        self.ensure_branch(branch)
        _run(["checkout", branch], self.root)

        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_text(encoding="utf-8") if target.exists() else None
        target.write_text(content, encoding="utf-8")

        _run(["add", relative_path], self.root)
        status = _run(["status", "--porcelain", "--", relative_path], self.root)

        parent_hash = self.head_commit(branch)
        diff = "" if previous is None else self._diff(parent_hash, target, previous, content)

        if not status:
            # Nothing changed — the working tree already matched `content`
            # (a retried job re-rendering the same spec). The existing commit
            # is the correct answer; report it rather than creating a
            # meaningless empty commit.
            return parent_hash, diff

        _run(["commit", "-m", message], self.root)
        return self.head_commit(branch), diff

    def _diff(self, parent_hash: str, target: Path, previous: str, content: str) -> str:
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

    def head_commit(self, branch: str) -> str:
        return _run(["rev-parse", branch], self.root)
