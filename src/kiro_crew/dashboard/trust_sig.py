"""Authenticated persistence of a chat session's auto-approve trust grant.

A dashboard "trust this session" / "trust read-only bash" grant, plus the
per-command fnmatch grants, are session-scoped auto-approve state. Persisting
them on the session's history meta line lets the grant survive a gateway
restart (the whole point) — but ``dashboard_*.jsonl`` lives in the same-uid,
**agent-writable** history dir (it is NOT on ``security._SENSITIVE_HOME_DIRS``),
so a bare ``"trust": true`` on the line is a grant an agent could WRITE FOR
ITSELF. Once any single arbitrary write is approved (e.g. via prompt injection),
the agent could edit its own session's meta line and the next routine restart
would silently convert that one approval into a durable, session-wide
auto-approve — disabling the containment prompt itself, not merely influencing
it. Type-validating the fields (bool / list-of-str) does not close this: a
well-formed ``true`` is exactly what the tamperer writes.

This module makes the persisted grant AUTHENTICATED, mirroring
``session_pid_sig`` (the same trust root, the same threat model):

* :func:`sign_trust` — the meta-line writer calls this to produce a
  ``trust_sig`` HMAC over the grant, bound to the session's history key and
  keyed with a subkey **derived from** the SEL trust root (``sel_hmac.key``,
  whose reads are deny-listed for agent shells in ``security.py``) via a
  domain-separation label. The raw root key never signs directly, so this
  protocol and the SEL audit chain never share a signing key.
* :func:`verify_trust` — the rehydrate path calls this. It returns the grant
  ONLY when the sidecar signature verifies against the same history key;
  missing/short key, absent signature, or mismatch all fail closed to "no
  trust". So a forged or tampered grant rehydrates as untrusted.

Why forgery dies: the agent cannot read ``sel_hmac.key`` (deny-listed), so it
cannot produce a valid ``trust_sig`` for a grant it writes into its own line.
Binding the history key into the MAC also stops replaying one session's signed
grant onto another session's line. Residual risk (an agent evading the deny-list
to read the key) is identical to the existing SEL tamper-evidence threat model.

Currency, not just origin — bounded by a signed TTL: the MAC would otherwise
authenticate *origin* without *currency*, so a grant signed once would be
honored identically forever, across unlimited restarts. To keep the durable
grant materially narrower than "trust this machine forever", the signer folds a
tamper-proof ``issued_at`` unix timestamp into the signed payload, and
:func:`verify_trust` refuses any grant older than :data:`_MAX_AGE_SECONDS`
(currently 7 days) or one dated in the future beyond a small clock-skew
allowance. Because ``issued_at`` is inside the MAC, an agent cannot forward-date
it to extend the window without the deny-listed root key; a stale grant simply
fails closed to no-trust and the user re-approves. This converts the feature
from "auto-approval survives restart forever" into "auto-approval survives
restart for a bounded window", which is the behavior a user actually expects and
a much smaller blast radius than an unbounded persisted grant.

Residual risk — same-window re-write: within that window an agent that RETAINED
its own session's earlier signed line could re-write it after a revocation and
regain trust until the TTL lapses (the signature stays genuine). The TTL caps
how long that can persist; fully closing it needs a non-agent-writable
revocation generation folded into the payload — the same class of residual as
the deny-list evasion above, and tracked as a follow-up.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Iterable

from kiro_crew.sel import _sel_hmac_key_bytes, sel_hmac_key_path

logger = logging.getLogger(__name__)

# Mirrors sel.py / session_pid_sig.py: a shorter key on disk means
# truncation/corruption/tampering; signing with it yields a predictable,
# forgeable MAC, so both sign and verify treat a short key as absent (fail
# closed on the verify side — no trust restored).
_HMAC_KEY_MIN_BYTES = 32

# Domain-separation label. ``sel_hmac.key`` anchors several independent
# protocols (the SEL audit chain, the session_pid sidecar, and now this trust
# sidecar). We NEVER sign with the raw root key: a purpose-specific subkey is
# derived via one HMAC step keyed by this label, so a MAC minted under one
# protocol can never be presented as valid for another. Versioned — bumping the
# suffix rotates every trust sidecar's effective key without touching the SEL
# root or the on-disk key file.
_SUBKEY_DOMAIN = b"kirocrew.session_trust.sig.v1"

# A restored grant is honored only within this window of its signed issued_at.
# Bounds the durable grant to "survives restart for about a week" rather than
# "forever across unlimited restarts" — the currency bound GPT's review calls
# for. 7 days is long enough that the persist-across-restart feature is useful
# (a routine restart mid-work keeps trust) yet finite. issued_at is inside the
# MAC, so an agent cannot extend it without the deny-listed root key.
_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
# Tolerate modest clock skew / non-monotonic wall clocks so a grant signed on a
# slightly-fast clock is not rejected as "future-dated"; beyond this a future
# issued_at is treated as malformed (fail closed).
_CLOCK_SKEW_SECONDS = 5 * 60


def _load_hmac_key() -> bytes | None:
    """Load the SEL trust-root key; None when absent/short (never creates).

    Prefers the on-disk FILE (the anchor every process resolves independently),
    falling back to the live ``SecurityEventLog`` singleton's validated in-memory
    copy — identical ordering to ``session_pid_sig._load_hmac_key``, which owns
    the rule that the file is preferred so a signer and a verifier never diverge.
    Only ``SecurityEventLog`` creates the key, so this never writes one.
    """
    try:
        raw = sel_hmac_key_path().read_bytes()
    except OSError:
        raw = b""
    if len(raw) >= _HMAC_KEY_MIN_BYTES:
        return raw
    return _sel_hmac_key_bytes()


def _derive_subkey(root: bytes) -> bytes:
    """Domain-separated one-way derivation of the trust-sidecar signing key."""
    return hmac.new(root, _SUBKEY_DOMAIN, hashlib.sha256).digest()


def _canonical_payload(
    history_key: str,
    *,
    trust: bool,
    trust_reads: bool,
    patterns: Iterable[str],
    issued_at: int,
) -> bytes:
    """Deterministic byte payload the MAC covers.

    Binds the SESSION (``history_key``) so a signed grant cannot be replayed
    onto another session's meta line, covers all three grant fields so flipping
    any of them (or adding a pattern) invalidates the signature, and binds
    ``issued_at`` so the freshness window cannot be forward-dated without the
    root key. Patterns are sorted and newline-joined; ``\\x1f`` (unit separator)
    delimits fields so a pattern containing the delimiter cannot shift framing.
    """
    pat = "\n".join(sorted(patterns))
    parts = [
        history_key,
        "1" if trust else "0",
        "1" if trust_reads else "0",
        pat,
        str(int(issued_at)),
    ]
    return "\x1f".join(parts).encode("utf-8")


def sign_trust(
    history_key: str,
    *,
    trust: bool,
    trust_reads: bool = False,
    patterns: Iterable[str] = (),
    issued_at: int | None = None,
) -> tuple[str, int] | None:
    """Return ``(trust_sig, issued_at)`` for a grant, or None when unsignable.

    ``issued_at`` is a unix timestamp folded into the MAC; the caller MUST
    persist the returned value alongside the signature so :func:`verify_trust`
    can re-check freshness. It defaults to "now" and is exposed as a parameter
    only so tests can sign a grant at a chosen age.

    None means the SEL trust root is absent/short in THIS process; the caller
    must then persist NO trust keys (an unsigned grant would fail verification
    and rehydrate as untrusted anyway, so writing it is pointless and would look
    like a silently-dropped grant). Never raises.
    """
    key = _load_hmac_key()
    if key is None:
        logger.debug(
            "session-trust signing unavailable (SEL trust root absent/short at %s); "
            "persisting grant unsigned is refused",
            sel_hmac_key_path(),
        )
        return None
    stamp = int(time.time()) if issued_at is None else int(issued_at)
    subkey = _derive_subkey(key)
    payload = _canonical_payload(
        history_key,
        trust=trust,
        trust_reads=trust_reads,
        patterns=patterns,
        issued_at=stamp,
    )
    return hmac.new(subkey, payload, hashlib.sha256).hexdigest(), stamp


def verify_trust(
    history_key: str,
    *,
    trust: bool,
    trust_reads: bool = False,
    patterns: Iterable[str] = (),
    signature: object,
    issued_at: object,
) -> bool:
    """True iff *signature* authenticates this grant for this session AND the
    grant is still within its freshness window.

    Fails closed (returns False) on: a non-string/empty signature; a missing or
    short SEL key; a MAC mismatch; a malformed/negative/future-dated
    ``issued_at``; or a grant older than :data:`_MAX_AGE_SECONDS`. The freshness
    check runs only AFTER the MAC verifies, so an attacker cannot probe the TTL
    boundary with forged timestamps — ``issued_at`` is inside the MAC, so any
    value that verifies is one the legitimate signer stamped. The caller then
    restores NO trust. Never raises.
    """
    if not isinstance(signature, str) or not signature:
        return False
    # issued_at must be an int-like unix stamp. bool is an int subclass but is
    # never a valid timestamp, so reject it explicitly.
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        return False
    key = _load_hmac_key()
    if key is None:
        # Distinct from a mismatch: the verifier's own trust root is
        # absent/short (e.g. a publisher/verifier split), NOT forgery. Fail
        # closed either way, but say which so a trust-root drift is diagnosable
        # rather than looking like universal tampering.
        logger.warning(
            "SEL trust-root key absent/short at %s — refusing persisted session "
            "trust (restores untrusted; if trust never survives restart, check "
            "for a signer/verifier trust-root split)",
            sel_hmac_key_path(),
        )
        return False
    subkey = _derive_subkey(key)
    payload = _canonical_payload(
        history_key,
        trust=trust,
        trust_reads=trust_reads,
        patterns=patterns,
        issued_at=issued_at,
    )
    expected = hmac.new(subkey, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    # MAC is authentic → issued_at is a value the signer stamped. Enforce the
    # bounded freshness window so a durable grant cannot outlive it.
    now = int(time.time())
    if issued_at > now + _CLOCK_SKEW_SECONDS:
        # Future-dated beyond skew: treat as malformed, fail closed.
        return False
    if now - issued_at > _MAX_AGE_SECONDS:
        logger.debug(
            "persisted session trust expired (issued_at=%d, age=%ds > %ds) — "
            "restoring untrusted; user re-approves",
            issued_at,
            now - issued_at,
            _MAX_AGE_SECONDS,
        )
        return False
    return True
