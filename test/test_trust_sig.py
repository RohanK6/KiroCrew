"""Unit tests for the authenticated session-trust sidecar (``trust_sig``).

Mirrors ``test_session_pid_sig``: a real ``sel_hmac.key`` under a tmp dir, the
canonical path accessor patched to it, and the in-memory fallback stubbed to
None so the FILE is the single source of truth in-test.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import trust_sig

HK = "dashboard:chat-1-1785"


@pytest.fixture
def sel_key(tmp_path):
    (tmp_path / "sel_hmac.key").write_bytes(b"\x07" * 32)
    with (
        patch.object(trust_sig, "sel_hmac_key_path", return_value=tmp_path / "sel_hmac.key"),
        patch.object(trust_sig, "_sel_hmac_key_bytes", return_value=None),
    ):
        yield tmp_path


class TestSignVerify:
    def test_round_trip(self, sel_key):
        sig, at = trust_sig.sign_trust(HK, trust=True, trust_reads=False, patterns={"git status"})
        assert isinstance(sig, str) and len(sig) == 64
        assert isinstance(at, int)
        assert trust_sig.verify_trust(
            HK,
            trust=True,
            trust_reads=False,
            patterns={"git status"},
            signature=sig,
            issued_at=at,
        )

    def test_pattern_order_irrelevant(self, sel_key):
        """A set signed in one order verifies in any order (canonical sorts)."""
        sig, at = trust_sig.sign_trust(HK, trust=True, patterns=["ls *", "git status"])
        assert trust_sig.verify_trust(
            HK,
            trust=True,
            trust_reads=False,
            patterns=["git status", "ls *"],
            signature=sig,
            issued_at=at,
        )

    def test_flip_trust_field_fails(self, sel_key):
        sig, at = trust_sig.sign_trust(HK, trust=False, trust_reads=True, patterns=())
        # Escalate trust=True: the MAC no longer matches.
        assert not trust_sig.verify_trust(
            HK, trust=True, trust_reads=True, patterns=(), signature=sig, issued_at=at
        )

    def test_add_pattern_fails(self, sel_key):
        sig, at = trust_sig.sign_trust(HK, trust=True, patterns={"git status"})
        assert not trust_sig.verify_trust(
            HK,
            trust=True,
            trust_reads=False,
            patterns={"git status", "rm -rf /"},
            signature=sig,
            issued_at=at,
        )

    def test_cross_session_replay_fails(self, sel_key):
        sig, at = trust_sig.sign_trust(HK, trust=True, patterns=())
        assert not trust_sig.verify_trust(
            "dashboard:other-session",
            trust=True,
            trust_reads=False,
            patterns=(),
            signature=sig,
            issued_at=at,
        )

    def test_empty_or_nonstring_signature_fails(self, sel_key):
        for bad in ("", None, 123, b"abc"):
            assert not trust_sig.verify_trust(
                HK,
                trust=True,
                trust_reads=False,
                patterns=(),
                signature=bad,
                issued_at=int(time.time()),
            )

    def test_guessed_signature_fails(self, sel_key):
        assert not trust_sig.verify_trust(
            HK,
            trust=True,
            trust_reads=False,
            patterns=(),
            signature="deadbeef" * 8,
            issued_at=int(time.time()),
        )


class TestFreshnessWindow:
    """issued_at is inside the MAC and re-checked as a bounded TTL, so a durable
    grant survives restart only within the window — not forever."""

    def test_recent_grant_verifies(self, sel_key):
        at = int(time.time()) - 60  # a minute old
        sig, _ = trust_sig.sign_trust(HK, trust=True, patterns=(), issued_at=at)
        assert trust_sig.verify_trust(
            HK, trust=True, trust_reads=False, patterns=(), signature=sig, issued_at=at
        )

    def test_grant_just_inside_window_verifies(self, sel_key):
        at = int(time.time()) - (trust_sig._MAX_AGE_SECONDS - 3600)  # 1h short of expiry
        sig, _ = trust_sig.sign_trust(HK, trust=True, patterns=(), issued_at=at)
        assert trust_sig.verify_trust(
            HK, trust=True, trust_reads=False, patterns=(), signature=sig, issued_at=at
        )

    def test_expired_grant_fails_closed(self, sel_key):
        at = int(time.time()) - (trust_sig._MAX_AGE_SECONDS + 3600)  # 1h past expiry
        sig, _ = trust_sig.sign_trust(HK, trust=True, patterns=(), issued_at=at)
        # Signature is genuine, but the grant is stale → restore nothing.
        assert not trust_sig.verify_trust(
            HK, trust=True, trust_reads=False, patterns=(), signature=sig, issued_at=at
        )

    def test_future_dated_beyond_skew_fails_closed(self, sel_key):
        at = int(time.time()) + trust_sig._CLOCK_SKEW_SECONDS + 3600
        sig, _ = trust_sig.sign_trust(HK, trust=True, patterns=(), issued_at=at)
        assert not trust_sig.verify_trust(
            HK, trust=True, trust_reads=False, patterns=(), signature=sig, issued_at=at
        )

    def test_forward_dating_issued_at_without_resigning_fails(self, sel_key):
        """An agent cannot extend the window by rewriting trust_at alone: the
        timestamp is inside the MAC, so a bumped value fails verification."""
        at = int(time.time()) - (trust_sig._MAX_AGE_SECONDS + 3600)  # expired
        sig, _ = trust_sig.sign_trust(HK, trust=True, patterns=(), issued_at=at)
        forged_now = int(time.time())  # try to present it as fresh
        assert not trust_sig.verify_trust(
            HK,
            trust=True,
            trust_reads=False,
            patterns=(),
            signature=sig,
            issued_at=forged_now,
        )

    def test_malformed_issued_at_fails_closed(self, sel_key):
        sig, at = trust_sig.sign_trust(HK, trust=True, patterns=())
        for bad in (None, "1785", 17.5, True, [at]):
            assert not trust_sig.verify_trust(
                HK,
                trust=True,
                trust_reads=False,
                patterns=(),
                signature=sig,
                issued_at=bad,
            )


class TestNoKey:
    def test_sign_returns_none_without_key(self, tmp_path):
        """No SEL key resolvable → sign_trust returns None (caller persists
        nothing) rather than emitting an unverifiable signature."""
        with (
            patch.object(trust_sig, "sel_hmac_key_path", return_value=tmp_path / "absent.key"),
            patch.object(trust_sig, "_sel_hmac_key_bytes", return_value=None),
        ):
            assert trust_sig.sign_trust(HK, trust=True, trust_reads=False, patterns=()) is None

    def test_short_key_treated_as_absent(self, tmp_path):
        (tmp_path / "sel_hmac.key").write_bytes(b"\x07" * 16)  # < 32
        with (
            patch.object(trust_sig, "sel_hmac_key_path", return_value=tmp_path / "sel_hmac.key"),
            patch.object(trust_sig, "_sel_hmac_key_bytes", return_value=None),
        ):
            assert trust_sig.sign_trust(HK, trust=True, trust_reads=False, patterns=()) is None

    def test_verify_fails_closed_without_key(self, tmp_path, sel_key):
        # Sign with a real key...
        sig, at = trust_sig.sign_trust(HK, trust=True, patterns=())
        # ...then verify in a process where the key is gone: fail closed.
        with (
            patch.object(trust_sig, "sel_hmac_key_path", return_value=tmp_path / "absent.key"),
            patch.object(trust_sig, "_sel_hmac_key_bytes", return_value=None),
        ):
            assert not trust_sig.verify_trust(
                HK, trust=True, trust_reads=False, patterns=(), signature=sig, issued_at=at
            )

    def test_in_memory_fallback_used_when_file_absent(self, tmp_path):
        """When the file is unreadable but the live SEL singleton has the key in
        memory, signing still works (mirrors session_pid_sig recovery)."""
        with (
            patch.object(trust_sig, "sel_hmac_key_path", return_value=tmp_path / "absent.key"),
            patch.object(trust_sig, "_sel_hmac_key_bytes", return_value=b"\x09" * 32),
        ):
            sig, at = trust_sig.sign_trust(HK, trust=True, patterns=())
            assert sig is not None
            assert trust_sig.verify_trust(
                HK, trust=True, trust_reads=False, patterns=(), signature=sig, issued_at=at
            )
