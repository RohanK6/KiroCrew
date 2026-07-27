"""Shared diagnostics / support-bundle collector.

Single code path behind both surfaces:

  * CLI  — ``kirocrew doctor --bundle``
  * UI   — Settings › About › "Report a Problem" (POST /api/diagnostics/collect)

The collector gathers the logs and crash reports needed to debug a user-reported
failure (the classic "process exited (rc=None)" ACP crash and friends), scrubs
every text member of secrets, zips them, and returns a :class:`BundleResult`
that carries a pre-filled GitHub issue URL the caller can open.

SECURITY: every text member is passed through the shared redaction pipeline
(``redact_exfiltration_urls`` then ``redact_credentials`` — same order used
everywhere else in the codebase) plus a small set of extra rules for the
patterns those two miss (``Bearer`` / ``Authorization`` headers, ``mc_token``
auth cookies, OAuth ``*_token`` JSON fields). Nothing is written to the zip
before it has been scrubbed. Sources that do not exist are skipped, never fatal.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from kiro_crew import __version__
from kiro_crew.config.loader import config_dir
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# ── GitHub issue target ──────────────────────────────────────────────────────
_ISSUE_REPO = "kirodotdev/KiroCrew"
_ISSUE_NEW_URL = f"https://github.com/{_ISSUE_REPO}/issues/new"

# Cap how many rolling files we pull so a huge crash-dump backlog can't bloat
# the bundle (or, worse, the redaction pass).
_MAX_CRASH_DUMPS = 5
_MAX_IPS_REPORTS = 5
# Keep only the newest N bundles in the output dir so a repeatedly-clicked
# "Report a Problem" can't grow the diagnostics dir unbounded.
_MAX_KEPT_BUNDLES = 10
# Per-member byte cap for text sources — tail the last N bytes of a giant log
# rather than shipping (and scrubbing) hundreds of MB.
_MAX_MEMBER_BYTES = 4 * 1024 * 1024


# ── Extra redaction rules ────────────────────────────────────────────────────
# ``redact_credentials`` / ``redact_exfiltration_urls`` do NOT cover live bearer
# tokens, Authorization headers, or the dashboard's ``mc_token`` auth cookie —
# all three appear verbatim in gateway.log / kiro-chat.log. Cover them here.
_EXTRA_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Sensitive HEADER LINES: redact the ENTIRE value to end-of-line. A header's
    # whole value is sensitive, so this deliberately over-redacts (safe) rather
    # than parsing it — that kills every delimiter/scheme edge case (comma-
    # delimited creds, `Basic <base64>`, multiple cookies on one Set-Cookie
    # line, etc.). Per-line via (?im) + ^…$.
    (
        re.compile(
            r"(?im)^([ \t]*(?:set-cookie|cookie|authorization|proxy-authorization|"
            r"www-authenticate|x-api-key|x-amz-security-token)[ \t]*[:=][ \t]*).+$"
        ),
        r"\1[REDACTED]",
    ),
    # Bare `Bearer <tok>` appearing OUTSIDE a header line (e.g. mid-prose, JSON).
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer [REDACTED]"),
    # Dashboard session/refresh cookies appearing outside a Cookie header line
    # (e.g. in a URL or JSON body): mc_token / mc_refresh (+ _<port>).
    (
        re.compile(r"(mc_(?:token|refresh)(?:_\d+)?\s*=\s*)[^\s;,\"']+"),
        r"\1[REDACTED]",
    ),
    # OAuth token fields in JSON / kv form: access_token / refresh_token / id_token
    (
        re.compile(
            r"(?i)([\"']?(?:access|refresh|id|session|api)[_-]?token[\"']?\s*[:=]\s*[\"']?)"
            r"[A-Za-z0-9._~+/=\-]{6,}"
        ),
        r"\1[REDACTED]",
    ),
)


@dataclass
class BundleResult:
    """Outcome of a :func:`collect_bundle` run."""

    zip_path: Path
    filename: str
    included: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: member name -> number of redactions applied (0 = clean)
    redaction_summary: dict[str, int] = field(default_factory=dict)
    github_issue_url: str = ""

    @property
    def total_redactions(self) -> int:
        return sum(self.redaction_summary.values())

    def as_dict(self) -> dict:
        return {
            "zip_path": str(self.zip_path),
            "filename": self.filename,
            "included": self.included,
            "skipped": self.skipped,
            "redaction_summary": self.redaction_summary,
            "total_redactions": self.total_redactions,
            "github_issue_url": self.github_issue_url,
        }


def _scrub(text: str) -> tuple[str, int]:
    """Run the full redaction pipeline over ``text``; return (clean, count)."""
    count = 0
    text, warnings = redact_exfiltration_urls(text)
    count += len(warnings)
    text, warnings = redact_credentials(text)
    count += len(warnings)
    for pattern, repl in _EXTRA_REDACTIONS:
        text, n = pattern.subn(repl, text)
        count += n
    return text, count


def _read_text_tail(path: Path, max_bytes: int = _MAX_MEMBER_BYTES) -> str:
    """Read a text file, keeping only the last ``max_bytes`` (logs grow at end)."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            raw = b"...[truncated: showing last %d bytes]...\n" % max_bytes + fh.read()
        else:
            raw = fh.read()
    return raw.decode("utf-8", errors="replace")


def _kiro_cli_chat_log() -> Path | None:
    """Locate the kiro-cli chat log under $TMPDIR/kiro-log/kiro-chat.log."""
    tmp = os.environ.get("TMPDIR") or "/tmp"
    p = Path(tmp) / "kiro-log" / "kiro-chat.log"
    return p if (p.is_file() and not p.is_symlink()) else None


def _kiro_cli_extra_logs() -> list[Path]:
    tmp = os.environ.get("TMPDIR") or "/tmp"
    base = Path(tmp) / "kiro-log"
    out: list[Path] = []
    for name in ("mcp.log", "lsp.log"):
        p = base / name
        if p.is_file() and not p.is_symlink() and p.stat().st_size > 0:
            out.append(p)
    return out


def _macos_crash_reports() -> list[Path]:
    """Newest kiro-cli / kiro .ips crash reports (macOS only)."""
    if sys.platform != "darwin":
        return []
    reports: list[Path] = []
    for base in (
        Path.home() / "Library" / "Logs" / "DiagnosticReports",
        Path("/Library/Logs/DiagnosticReports"),
    ):
        if not base.is_dir():
            continue
        try:
            reports.extend(
                p
                for p in base.glob("kiro*.ips")
                if p.is_file() and not p.is_symlink()
            )
        except OSError:
            continue
    reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[:_MAX_IPS_REPORTS]


def _kiro_cli_version() -> str:
    try:
        out = subprocess.run(
            ["kiro-cli", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (out.stdout or out.stderr).strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def _channel() -> str:
    if "-nightly." in __version__:
        return "nightly"
    if "-" in __version__:
        return "prerelease"
    return "stable"


def _versions_text(note: str) -> str:
    lines = [
        f"kirocrew_version: {__version__}",
        f"channel: {_channel()}",
        f"kiro_cli_version: {_kiro_cli_version()}",
        f"python: {platform.python_version()}",
        f"platform: {platform.platform()}",
        f"machine: {platform.machine()}",
        f"collected_at: {datetime.now(timezone.utc).isoformat()}",
        f"data_home: {config_dir()}",
    ]
    if note.strip():
        lines.append("")
        lines.append("user_note:")
        lines.append(note.strip())
    return "\n".join(lines) + "\n"


def _issue_url(result: BundleResult, note: str) -> str:
    title = "[bug] process exited / chat failure"
    body = "\n".join(
        [
            "## What happened",
            note.strip() or "_(describe the problem here)_",
            "",
            "## Environment",
            f"- KiroCrew: `{__version__}` ({_channel()})",
            f"- kiro-cli: `{_kiro_cli_version()}`",
            f"- OS: `{platform.platform()}`",
            "",
            "## Diagnostics",
            f"Attach the diagnostics bundle: `{result.filename}`",
            f"(saved locally at `{result.zip_path}` — {result.total_redactions} "
            "secret(s) auto-redacted before packaging).",
            "",
            "<!-- Drag the .zip into this issue before submitting. -->",
        ]
    )
    return f"{_ISSUE_NEW_URL}?" + urlencode({"title": title, "body": body})


def _prune_old_bundles(out_dir: Path, keep: int) -> None:
    """Keep only the newest ``keep`` diagnostics zips in ``out_dir``."""
    try:
        bundles = sorted(
            out_dir.glob("kirocrew-diagnostics-*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in bundles[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def collect_bundle(
    *,
    note: str = "",
    include_logs: bool = True,
    output_dir: Path | None = None,
) -> BundleResult:
    """Collect, redact, and zip a diagnostics bundle.

    Args:
        note: optional free-text description from the user (kept in the bundle
            and pre-filled into the GitHub issue body).
        include_logs: when False, only versions + crash reports are bundled
            (no full gateway / chat logs) for a lighter, lower-sensitivity zip.
        output_dir: where to write the zip. Defaults to ``<data_home>/diagnostics``.

    Returns:
        :class:`BundleResult` with the zip path and a pre-filled issue URL.
    """
    home = config_dir()
    out_dir = output_dir or (home / "diagnostics")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out_dir, 0o700)  # bundles hold (redacted) local diagnostic logs
    except OSError:
        pass

    # Scrub the user-supplied note ONCE up front. It flows into versions.txt,
    # manifest.json, AND the pre-filled GitHub issue URL — a user may paste a
    # secret (bearer token, key) into "what happened?", so it needs the same
    # redaction the log members get below.
    note, _ = _scrub(note or "")

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    filename = f"kirocrew-diagnostics-{stamp}-{uuid.uuid4().hex[:8]}.zip"
    zip_path = out_dir / filename

    result = BundleResult(zip_path=zip_path, filename=filename)

    # (member_name, source_path, gated_by_include_logs)
    text_sources: list[tuple[str, Path, bool]] = []
    if include_logs:
        text_sources.append(("gateway.log", home / "gateway.log", True))
        text_sources.append(("gateway.log.prev", home / "gateway.log.prev", True))
        chat = _kiro_cli_chat_log()
        if chat is not None:
            text_sources.append(("kiro-chat.log", chat, True))
        for extra in _kiro_cli_extra_logs():
            text_sources.append((f"kiro-cli-{extra.name}", extra, True))

    # Crash artifacts are always useful and low-volume — include regardless.
    text_sources.append(("crash.log", home / "logs" / "crash.log", False))
    dumps_dir = home / "logs" / "crash-dumps"
    if dumps_dir.is_dir():
        dumps = sorted(
            (p for p in dumps_dir.glob("*") if p.is_file() and not p.is_symlink()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:_MAX_CRASH_DUMPS]
        for d in dumps:
            text_sources.append((f"crash-dumps/{d.name}", d, False))
    for ips in _macos_crash_reports():
        text_sources.append((f"crash-reports/{ips.name}", ips, False))

    # Create the archive 0o600 from the start (not chmod-after, which leaves a
    # world-readable window) — the bundle holds local diagnostic logs.
    _fd = os.open(zip_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(_fd, "wb") as _raw, zipfile.ZipFile(
        _raw, "w", zipfile.ZIP_DEFLATED
    ) as zf:
        # Generated members first.
        versions = _versions_text(note)
        zf.writestr("versions.txt", versions)
        result.included.append("versions.txt")
        result.redaction_summary["versions.txt"] = 0

        for member, src, _gated in text_sources:
            try:
                # Never follow a symlink — a symlinked source could pull an
                # arbitrary off-tree target (e.g. ~/.ssh/id_rsa) into the bundle.
                if src.is_symlink() or not src.is_file():
                    result.skipped.append(member)
                    continue
                text = _read_text_tail(src)
            except OSError:
                result.skipped.append(member)
                continue
            clean, n = _scrub(text)
            zf.writestr(member, clean)
            result.included.append(member)
            result.redaction_summary[member] = n

        # Manifest last so it reflects the final included/skipped/redaction state.
        manifest = {
            "tool": "kirocrew-diagnostics",
            "kirocrew_version": __version__,
            "channel": _channel(),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "include_logs": include_logs,
            "note": note.strip(),
            "included": result.included,
            "skipped": result.skipped,
            "redaction_summary": result.redaction_summary,
            "total_redactions": result.total_redactions,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        result.included.append("manifest.json")

    result.github_issue_url = _issue_url(result, note)
    _prune_old_bundles(out_dir, _MAX_KEPT_BUNDLES)
    return result
