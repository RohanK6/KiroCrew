#!/usr/bin/env python3
"""push_guard.py - pre-push stale-base guard for the prepare-pr skill.

Supports two invocation modes matching the prepare-pr workflow phases:

**Pre-squash (Phase 1.3):** default mode — verifies that the branch history is
safe BEFORE the squash destroys the commit-count signal:
1. The fetch of origin/<base> succeeds (fail closed on network error).
2. The number of commits HEAD is ahead of origin/<base> is plausibly small
   (default threshold: 5 commits; configurable via --max-ahead).

This prevents the catastrophic failure mode where a worktree branched from a
stale local integration trunk carries 100+ unshipped commits that, after squash,
become invisible (count=1) and get force-pushed to the remote feature branch.

**Post-squash (Phase 3.1 with --require-single-on-base):** verifies structural
correctness of the squashed commit:
1. The fetch of origin/<base> succeeds (fail closed on network error).
2. HEAD~1 (the parent of the single squashed commit) equals origin/<base> after
   the fresh fetch — i.e. the squashed commit sits directly on the remote base.

This is the only meaningful post-squash invariant: once squashed to one commit,
rev-list count is always 1, so --max-ahead cannot detect a problem. Instead,
the structural check catches the case where the squash landed on a stale ref
(e.g. origin/<base> advanced between the rebase and the push).

Portable: stdlib only; shells out to git via argument lists.

Usage:
  Pre-squash:  python3 push_guard.py [--base <branch>] [--max-ahead <N>]
  Post-squash: python3 push_guard.py [--base <branch>] --require-single-on-base

Exit:   0 SAFE | 40 REFUSED (stale base / structural mismatch) | 2 env error
"""
import argparse
import subprocess
import sys


def run(args):
    """Run a command; return (returncode, stdout, stderr) as stripped text."""
    try:
        p = subprocess.run(args, capture_output=True, text=True)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def _resolve_base(explicit_base):
    """Resolve the base branch name from explicit arg, origin/HEAD, or 'main'."""
    if explicit_base:
        return explicit_base
    sym = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])[1]
    if sym.startswith("origin/"):
        return sym[len("origin/") :]
    return "main"


def _fetch_base(base):
    """Fetch origin/<base>; return 0 on success, 40 on failure."""
    print("Fetching origin/{} ...".format(base))
    fetch_rc, _, fetch_err = run(["git", "fetch", "--quiet", "origin", base])
    if fetch_rc != 0:
        err(
            "REFUSED: git fetch origin {} failed. Cannot verify base "
            "freshness — refusing to push on a potentially stale ref.\n"
            "  error: {}".format(base, fetch_err[:300])
        )
        return 40
    return 0


def _check_single_on_base(base):
    """Post-squash structural check: HEAD~1 must equal fresh origin/<base>.

    After a squash to one commit, the commit-count heuristic (--max-ahead) is
    always 1 and provides no signal. The structural invariant is stronger: the
    single squashed commit must sit directly on the fresh remote base. If it
    does not, either the squash landed on a stale ref or the branch carries
    unexpected history.
    """
    # Resolve HEAD~1 (parent of the single squashed commit).
    rc, parent_sha, _ = run(["git", "rev-parse", "HEAD~1"])
    if rc != 0:
        err(
            "REFUSED: cannot resolve HEAD~1. The branch may have no parent "
            "(initial commit or orphan). Cannot verify single-on-base."
        )
        return 40

    # Resolve origin/<base> after the fresh fetch.
    rc, origin_base_sha, _ = run(["git", "rev-parse", "origin/{}".format(base)])
    if rc != 0:
        err("REFUSED: cannot resolve origin/{} after fetch.".format(base))
        return 40

    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("HEAD:            " + head_sha)
    print("HEAD~1 (parent): " + parent_sha[:12])
    print("origin/{}:     {}".format(base, origin_base_sha[:12]))

    if parent_sha != origin_base_sha:
        err(
            "REFUSED: HEAD~1 ({}) != origin/{} ({}). "
            "The squashed commit does not sit directly on the fresh remote "
            "base. Either the squash landed on a stale ref (origin/{} moved "
            "between rebase and push) or the branch carries unexpected "
            "history. Re-sync: fetch, rebase, re-squash.".format(
                parent_sha[:12], base, origin_base_sha[:12], base
            )
        )
        return 40

    print("STATUS: SAFE TO PUSH (single commit directly on fresh origin/{})".format(base))
    return 0


def _check_pre_squash(base, max_ahead):
    """Pre-squash count-based check: commit count must be <= max_ahead.

    Before the squash, rev-list --count reflects the branch's real history.
    A polluted trunk (branched from a stale local integration branch) shows
    100+ commits here, tripping the threshold. Also verifies that HEAD shares
    common history with origin/<base> (catches orphan branches that have no
    merge-base at all).
    """
    # Verify HEAD shares common history with origin/<base>.
    rc, merge_base, _ = run(["git", "merge-base", "HEAD", "origin/{}".format(base)])
    if rc != 0 or not merge_base:
        err(
            "REFUSED: cannot compute merge-base between HEAD and origin/{}. "
            "The branch may have no common history with the remote base.".format(base)
        )
        return 40

    # Count commits HEAD is ahead of origin/<base>.
    rc, count_str, _ = run(["git", "rev-list", "--count", "origin/{}..HEAD".format(base)])
    if rc != 0:
        err("REFUSED: cannot count commits ahead of origin/{}.".format(base))
        return 40

    try:
        ahead = int(count_str)
    except ValueError:
        err("REFUSED: unexpected rev-list output: {}".format(count_str))
        return 40

    origin_base_sha = run(["git", "rev-parse", "origin/{}".format(base)])[1][:12]
    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("merge-base:      " + merge_base[:12])
    print("origin/{}:     {}".format(base, origin_base_sha))
    print("HEAD:            " + head_sha)
    print("commits ahead:   {}".format(ahead))
    print("max allowed:     {}".format(max_ahead))

    if ahead > max_ahead:
        err(
            "REFUSED: HEAD is {} commits ahead of origin/{} (max allowed: {}). "
            "This is far too many for a squashed single-commit PR — the branch "
            "likely carries unshipped local integration commits that would "
            "clobber upstream work if force-pushed.\n"
            "  To fix: git rebase origin/{} to rebase only your changes onto "
            "the fresh remote base, or reset to origin/{} and cherry-pick your "
            "commit.".format(ahead, base, max_ahead, base, base)
        )
        return 40

    print("STATUS: SAFE TO PUSH")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Pre-push stale-base guard")
    parser.add_argument(
        "--base",
        default="",
        help="Base branch name (without origin/ prefix). "
        "Auto-detected from PR or origin/HEAD if omitted.",
    )
    parser.add_argument(
        "--max-ahead",
        type=int,
        default=5,
        help="Maximum commits HEAD may be ahead of origin/<base> (default: 5). "
        "Used in pre-squash mode only.",
    )
    parser.add_argument(
        "--require-single-on-base",
        action="store_true",
        help="Post-squash mode: assert HEAD~1 == origin/<base> after a fresh "
        "fetch. Use after squashing to one commit (Phase 3) instead of the "
        "commit-count check which is meaningless post-squash.",
    )
    args = parser.parse_args()

    # Must be in a git repo.
    if run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
        err("ERROR: not inside a git repository.")
        return 2

    base = _resolve_base(args.base)

    # Step 1: Fetch origin/<base> — MUST succeed (fail closed).
    fetch_result = _fetch_base(base)
    if fetch_result != 0:
        return fetch_result

    # Dispatch to the appropriate check mode.
    if args.require_single_on_base:
        return _check_single_on_base(base)
    else:
        return _check_pre_squash(base, args.max_ahead)


if __name__ == "__main__":
    sys.exit(main())
