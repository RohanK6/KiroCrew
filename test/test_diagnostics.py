"""Tests for the diagnostics collector engine + dashboard API handlers.

Focus areas:
  * secrets are scrubbed from every text member before zipping (security-critical)
  * missing sources are skipped, never fatal
  * include_logs=False produces a lighter bundle
  * the pre-filled GitHub issue URL is well-formed
  * the download handler rejects path traversal / non-zip names
  * the collect handler returns a download_url + issue url
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from kiro_crew import diagnostics
from kiro_crew.dashboard.handlers import diagnostics as dh
from kiro_crew.diagnostics import BundleResult

_GATEWAY = (
    "09:00 boot ok\n"
    "Authorization: Bearer sk-ant-SECRETtoken1234567890abcXYZ\n"
    "Set-Cookie: mc_token_5476=supersecretcookievalueABCDEF123456\n"
    "Set-Cookie: mc_refresh_5476=REFRESHsecretVALUE9876543210\n"
    "aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY\n"
    "a perfectly normal log line\n"
)

_SECRETS = (
    "sk-ant-SECRETtoken1234567890abcXYZ",
    "supersecretcookievalueABCDEF123456",
    "REFRESHsecretVALUE9876543210",
    "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
)


def _isolate(monkeypatch, home: Path) -> None:
    """Point the collector at a temp home and stub host-specific probes."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("kiro_crew.diagnostics.config_dir", lambda: home)
    monkeypatch.setattr(diagnostics, "_macos_crash_reports", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: None)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_version", lambda: "kiro-cli 2.14.2")


def test_collect_bundle_redacts_and_zips(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(_GATEWAY)

    r = diagnostics.collect_bundle(note="every message fails", output_dir=tmp_path / "out")

    assert r.zip_path.is_file()
    with zipfile.ZipFile(r.zip_path) as z:
        names = set(z.namelist())
        assert {"versions.txt", "manifest.json", "gateway.log"} <= names
        gw = z.read("gateway.log").decode()
        manifest = json.loads(z.read("manifest.json"))

    for secret in _SECRETS:
        assert secret not in gw, f"secret leaked into bundle: {secret!r}"
    assert "a perfectly normal log line" in gw
    assert r.total_redactions >= 3
    assert r.redaction_summary["gateway.log"] >= 3
    assert manifest["total_redactions"] == r.total_redactions
    assert manifest["note"] == "every message fails"


def test_authorization_scheme_credential_fully_redacted(tmp_path, monkeypatch):
    """A non-Bearer scheme + raw token must be fully redacted (not just the scheme)."""
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(
        "Authorization: Token abc-123-def-456-ghijklmno\nplain\n"
    )
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()
    assert "abc-123-def-456-ghijklmno" not in gw
    assert "[REDACTED]" in gw


def test_authorization_comma_delimited_fully_redacted(tmp_path, monkeypatch):
    """Comma-delimited creds on one Authorization line must ALL be redacted."""
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(
        "Authorization: OAuth oauth_token=LEAKtok111abc, "
        "oauth_signature=SIGsecret222xyz\nplain\n"
    )
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()
    assert "LEAKtok111abc" not in gw
    assert "SIGsecret222xyz" not in gw
    assert "plain" in gw


def test_symlinked_source_is_not_followed(tmp_path, monkeypatch):
    """A symlinked source must be skipped, not followed to an off-tree target."""
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    secret = tmp_path / "off_tree_secret.txt"
    secret.write_text("TOPSECRETsymlinkVALUE123")
    (home / "gateway.log").symlink_to(secret)
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    assert "gateway.log" in r.skipped
    with zipfile.ZipFile(r.zip_path) as z:
        assert "gateway.log" not in z.namelist()
        joined = "".join(z.read(n).decode(errors="replace") for n in z.namelist())
    assert "TOPSECRETsymlinkVALUE123" not in joined


def test_archive_is_not_world_readable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text("ok\n")
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    mode = r.zip_path.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_missing_sources_are_skipped_not_fatal(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)  # no gateway.log written

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    assert r.zip_path.is_file()
    assert "gateway.log" in r.skipped
    assert "versions.txt" in r.included
    assert "manifest.json" in r.included


def test_include_logs_false_excludes_gateway(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(_GATEWAY)

    r = diagnostics.collect_bundle(include_logs=False, output_dir=tmp_path / "out")

    with zipfile.ZipFile(r.zip_path) as z:
        assert "gateway.log" not in z.namelist()
        assert "versions.txt" in z.namelist()


def test_issue_url_is_well_formed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)

    r = diagnostics.collect_bundle(note="hi", output_dir=tmp_path / "out")

    assert r.github_issue_url.startswith(
        "https://github.com/kirodotdev/KiroCrew/issues/new?"
    )
    assert "title=" in r.github_issue_url
    assert "body=" in r.github_issue_url


# ── API handlers (mode-independent: stub request + asyncio.run) ──────────────


class _DownloadReq:
    def __init__(self, filename: str) -> None:
        self.match_info = {"filename": filename}


class _CollectReq:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


def _stub_sel(monkeypatch) -> MagicMock:
    """Install a mock SEL logger and return it (download handler audits via it)."""
    sel = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: sel)
    return sel


def test_download_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    sel = _stub_sel(monkeypatch)
    resp = asyncio.run(dh.api_diagnostics_download(_DownloadReq("../../etc/passwd")))
    assert resp.status == 403
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "denied"


def test_download_rejects_non_zip(tmp_path, monkeypatch):
    diag = tmp_path / "diagnostics"
    diag.mkdir()
    (diag / "foo.txt").write_text("not a zip")
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    sel = _stub_sel(monkeypatch)
    resp = asyncio.run(dh.api_diagnostics_download(_DownloadReq("foo.txt")))
    assert resp.status == 403
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "denied"


def test_download_allows_and_audits_valid_zip(tmp_path, monkeypatch):
    diag = tmp_path / "diagnostics"
    diag.mkdir()
    (diag / "b.zip").write_bytes(b"PK\x03\x04zip")
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    sel = _stub_sel(monkeypatch)
    resp = asyncio.run(dh.api_diagnostics_download(_DownloadReq("b.zip")))
    assert resp.status == 200
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "allowed"


def test_collect_handler_returns_download_url(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    fake = BundleResult(
        zip_path=tmp_path / "b.zip",
        filename="b.zip",
        included=["versions.txt", "manifest.json"],
        skipped=[],
        redaction_summary={"versions.txt": 0},
        github_issue_url="https://github.com/kirodotdev/KiroCrew/issues/new?title=x",
    )
    monkeypatch.setattr(dh.diagnostics, "collect_bundle", lambda **kw: fake)

    resp = asyncio.run(
        dh.api_diagnostics_collect(_CollectReq({"note": "hi", "include_logs": True}))
    )
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["download_url"] == "/api/diagnostics/download/b.zip"
    assert body["github_issue_url"].startswith("https://github.com/")
    assert body["filename"] == "b.zip"


def test_note_is_redacted_everywhere(tmp_path, monkeypatch):
    """A secret pasted into the note must not survive into any output."""
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    secret = "Bearer sk-ant-NOTEsecretVALUE1234567890"
    r = diagnostics.collect_bundle(
        note=f"it broke, my log had {secret} in it",
        output_dir=tmp_path / "out",
    )
    with zipfile.ZipFile(r.zip_path) as z:
        versions = z.read("versions.txt").decode()
        manifest = z.read("manifest.json").decode()
    assert "NOTEsecretVALUE1234567890" not in versions
    assert "NOTEsecretVALUE1234567890" not in manifest
    assert "NOTEsecretVALUE1234567890" not in r.github_issue_url


def test_old_bundles_are_pruned(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    base = time.time()
    for i in range(6):
        p = out / f"kirocrew-diagnostics-2026010{i}-000000.zip"
        p.write_bytes(b"x")
        os.utime(p, (base + i, base + i))  # distinct mtimes; newest = i==5
    diagnostics._prune_old_bundles(out, keep=3)
    kept = {p.name for p in out.glob("kirocrew-diagnostics-*.zip")}
    assert len(kept) == 3
    assert "kirocrew-diagnostics-20260105-000000.zip" in kept  # newest kept
    assert "kirocrew-diagnostics-20260100-000000.zip" not in kept  # oldest pruned


def test_collect_handler_rejects_non_object_body(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    resp = asyncio.run(dh.api_diagnostics_collect(_CollectReq(["not", "a", "dict"])))
    assert resp.status == 400
