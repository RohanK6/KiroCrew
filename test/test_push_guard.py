"""Tests for the prepare-pr push_guard.py stale-base detection.

Verifies that the push_guard script (src/kiro_crew/builtin_skills/kirocrew-dev/
prepare-pr/scripts/push_guard.py) correctly refuses to push when:
- The merge-base is not an ancestor of origin/<base> (stale local trunk)
- The commit count exceeds --max-ahead (implausibly many commits for a PR)
- The fetch of origin/<base> fails (network error → fail closed)

And allows push when the branch is a normal single-commit PR (1 commit ahead
of a fresh origin/<base> whose merge-base is an ancestor of origin/<base>).

Regression test for the 2026-07-31 clobber incident: a force-push from a
worktree branched off kiki-trunk carried 114 duplicate commits.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Resolve the push_guard.py script path relative to the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH_GUARD = str(
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "push_guard.py"
)


def _run_push_guard(cwd: str, extra_args: list[str] | None = None) -> tuple[int, str, str]:
    """Run push_guard.py in the given directory; return (rc, stdout, stderr)."""
    args = [sys.executable, PUSH_GUARD, "--base", "main"]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git(cwd: str, *args: str) -> str:
    """Run a git command in cwd; raise on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo_pair(tmp_path):
    """Create a local 'origin' bare repo and a working clone.

    Returns (clone_dir, origin_dir) where origin_dir is a bare repo and
    clone_dir has 'origin' pointing at origin_dir.
    """
    origin_dir = str(tmp_path / "origin.git")
    clone_dir = str(tmp_path / "work")

    # Create a bare origin with one commit on main.
    os.makedirs(origin_dir)
    _git(origin_dir, "init", "--bare")
    _git(origin_dir, "symbolic-ref", "HEAD", "refs/heads/main")

    # Clone it.
    _git(str(tmp_path), "clone", origin_dir, "work")
    _git(clone_dir, "checkout", "-b", "main")

    # Create an initial commit on main.
    Path(clone_dir, "README.md").write_text("initial\n")
    _git(clone_dir, "add", "README.md")
    _git(clone_dir, "commit", "-m", "initial commit")
    _git(clone_dir, "push", "-u", "origin", "main")

    return clone_dir, origin_dir


class TestPushGuardSafe:
    """Normal single-commit PR: push_guard exits 0 (safe)."""

    def test_single_commit_ahead(self, repo_pair):
        clone_dir, _ = repo_pair

        # Create a feature branch with one commit ahead of origin/main.
        _git(clone_dir, "checkout", "-b", "feature/my-fix")
        Path(clone_dir, "fix.py").write_text("# fix\n")
        _git(clone_dir, "add", "fix.py")
        _git(clone_dir, "commit", "-m", "fix: the bug")

        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout

    def test_max_ahead_at_threshold(self, repo_pair):
        """Exactly at --max-ahead=3 should pass."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/multi")
        for i in range(3):
            Path(clone_dir, f"file{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"file{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "3"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout


class TestPushGuardRefused:
    """push_guard exits 40 (refused) when the branch is unsafe to push."""

    def test_too_many_commits_ahead(self, repo_pair):
        """Branch with 6 commits and --max-ahead=5 → refused."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/bloated")
        for i in range(6):
            Path(clone_dir, f"file{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"file{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "6 commits ahead" in stderr

    def test_merge_base_not_ancestor_of_origin(self, repo_pair):
        """Merge-base is NOT an ancestor of origin/main (diverged history)."""
        clone_dir, origin_dir = repo_pair

        # Simulate a stale trunk: create a commit that diverges from origin/main.
        # First, advance origin/main with a new commit.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream.txt").write_text("upstream change\n")
        _git(work2, "add", "upstream.txt")
        _git(work2, "commit", "-m", "upstream: new feature")
        _git(work2, "push", "origin", "main")

        # Now in the original clone, DON'T fetch, create a branch from stale
        # main with an orphan commit structure.
        _git(clone_dir, "checkout", "--orphan", "stale-trunk")
        Path(clone_dir, "stale.txt").write_text("stale\n")
        _git(clone_dir, "add", "stale.txt")
        _git(clone_dir, "commit", "-m", "stale trunk commit")

        # The merge-base between this orphan and origin/main won't exist.
        rc, stdout, stderr = _run_push_guard(clone_dir)
        # Should be refused: either no merge-base (orphan) or not ancestor.
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr

    def test_fetch_failure_refuses(self, tmp_path):
        """When origin doesn't have the base branch, fetch fails → refused."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Set origin to a non-existent path so fetch always fails.
        _git(repo_dir, "remote", "add", "origin", "/nonexistent/repo.git")

        rc, stdout, stderr = _run_push_guard(repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "fetch" in stderr.lower()

    def test_stale_base_clobber_scenario(self, repo_pair):
        """Reproduce the exact clobber pattern: many commits from a local trunk
        that aren't on the remote."""
        clone_dir, origin_dir = repo_pair

        # Simulate kiki-trunk: advance local main with 10 "integration" commits
        # that never get pushed to origin.
        _git(clone_dir, "checkout", "main")
        for i in range(10):
            Path(clone_dir, f"integration{i}.py").write_text(f"# int {i}\n")
            _git(clone_dir, "add", f"integration{i}.py")
            _git(clone_dir, "commit", "-m", f"feat(integration): commit {i}")

        # Branch from the stale local main (as kiki does from kiki-trunk).
        _git(clone_dir, "checkout", "-b", "feature/pr-fix")
        Path(clone_dir, "fix.py").write_text("# fix\n")
        _git(clone_dir, "add", "fix.py")
        _git(clone_dir, "commit", "-m", "fix: the issue")

        # Now this branch is 11 commits ahead of origin/main (10 integration +
        # 1 actual fix). The push_guard MUST refuse.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "11 commits ahead" in stderr


class TestPushGuardEdgeCases:
    """Edge cases and error handling."""

    def test_not_a_git_repo(self, tmp_path):
        """Running outside a git repo → exit 2."""
        rc, stdout, stderr = _run_push_guard(str(tmp_path))
        assert rc == 2

    def test_custom_max_ahead(self, repo_pair):
        """--max-ahead=1 catches even 2 commits."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/small")
        for i in range(2):
            Path(clone_dir, f"f{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"f{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "1"])
        assert rc == 40
        assert "2 commits ahead" in stderr


class TestRequireSingleOnBase:
    """Post-squash structural check: --require-single-on-base.

    Regression tests for the guard-before-squash fix (PR #1418): the pre-squash
    guard catches polluted history via commit count, but post-squash that signal
    is destroyed (count is always 1). The structural check asserts HEAD~1 equals
    origin/<base> after a fresh fetch — the only meaningful post-squash invariant.
    """

    def test_squashed_on_fresh_base_passes(self, repo_pair):
        """A single squashed commit directly on fresh origin/main → safe."""
        clone_dir, _ = repo_pair

        # Create a feature branch and squash to one commit on main.
        _git(clone_dir, "checkout", "-b", "feature/clean-squash")
        Path(clone_dir, "feature.py").write_text("# feature\n")
        _git(clone_dir, "add", "feature.py")
        _git(clone_dir, "commit", "-m", "feat: add feature")

        # Squash: reset --soft to origin/main and recommit.
        _git(clone_dir, "reset", "--soft", "origin/main")
        _git(clone_dir, "commit", "-m", "feat: squashed feature")

        # HEAD~1 should now equal origin/main.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout

    def test_squashed_off_stale_base_refused(self, repo_pair):
        """Squashed commit whose parent is NOT fresh origin/main → refused.

        Simulates the failure mode: origin/main advances after the local squash,
        so HEAD~1 no longer matches the fresh origin/main. The structural check
        catches this even though commit count is 1.
        """
        clone_dir, origin_dir = repo_pair

        # Create a feature branch and squash to one commit on current origin/main.
        _git(clone_dir, "checkout", "-b", "feature/stale-squash")
        Path(clone_dir, "feature.py").write_text("# feature\n")
        _git(clone_dir, "add", "feature.py")
        _git(clone_dir, "commit", "-m", "feat: add feature")
        _git(clone_dir, "reset", "--soft", "origin/main")
        _git(clone_dir, "commit", "-m", "feat: squashed feature")

        # Now advance origin/main (simulating base movement between rebase and push).
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream_advance.txt").write_text("new upstream commit\n")
        _git(work2, "add", "upstream_advance.txt")
        _git(work2, "commit", "-m", "chore: upstream advance")
        _git(work2, "push", "origin", "main")

        # HEAD~1 is the OLD origin/main; after fresh fetch, origin/main moved.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "does not sit directly on the fresh remote base" in stderr

    def test_polluted_history_pre_squash_refused(self, repo_pair):
        """Pre-squash guard catches polluted history BEFORE squash destroys signal.

        Regression test: this is the exact scenario the GPT finding identified.
        A branch carries many commits from a stale local trunk. Running the guard
        pre-squash (without --require-single-on-base) correctly refuses because
        commit count exceeds --max-ahead. After squash, count would be 1 and the
        old guard (post-squash only) would have missed it.
        """
        clone_dir, _ = repo_pair

        # Simulate polluted trunk: advance local main with integration commits.
        _git(clone_dir, "checkout", "main")
        for i in range(15):
            Path(clone_dir, f"integration{i}.py").write_text(f"# int {i}\n")
            _git(clone_dir, "add", f"integration{i}.py")
            _git(clone_dir, "commit", "-m", f"feat(integration): commit {i}")

        # Branch from the polluted local main.
        _git(clone_dir, "checkout", "-b", "feature/from-polluted-trunk")
        Path(clone_dir, "my_fix.py").write_text("# fix\n")
        _git(clone_dir, "add", "my_fix.py")
        _git(clone_dir, "commit", "-m", "fix: the issue")

        # Pre-squash: 16 commits ahead (15 integration + 1 fix) → refused.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "16 commits ahead" in stderr

    def test_multiple_commits_with_require_single_on_base(self, repo_pair):
        """Multiple unsquashed commits with --require-single-on-base → refused.

        If someone accidentally runs the post-squash check without squashing first,
        HEAD~1 won't match origin/main (it'll be a prior feature commit).
        """
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/not-squashed")
        for i in range(3):
            Path(clone_dir, f"f{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"f{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        # HEAD~1 is the 2nd feature commit, not origin/main.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
