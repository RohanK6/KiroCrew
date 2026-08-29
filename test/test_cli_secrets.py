"""Tests for kirocrew secrets CLI subcommands."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.secrets import SecretVault


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    """Create a temporary vault with test secrets."""
    vault = SecretVault(tmp_path)
    vault._set_sync("API_KEY", "sk-test-12345")
    vault._set_sync("DB_PASS", "hunter2")
    return tmp_path


@pytest.fixture()
def empty_vault_dir(tmp_path: Path) -> Path:
    """Config dir with no vault."""
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_spawned_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the session reads as a HUMAN one for CLI tests.

    The mutating ``secrets`` verbs are gated by
    ``session_pid.agent_session_blocks_vault_mutation()``. Its env-var clause is
    what most gate tests drive, so we do NOT stub the gate itself — we only
    neutralize the two host-dependent inputs it also consults: force
    ``ancestry_inspection_supported`` True (so the gate does not fail closed on a
    non-Linux CI box) and ``agent_marker_in_ancestry`` False (so a CI runner
    whose own ancestry happens to carry the marker does not leak in). With the
    env var cleared, the gate reads "human" by default; a test that sets
    ``KIROCREW_SPAWNED`` still exercises the real block.
    """
    monkeypatch.delenv("KIROCREW_SPAWNED", raising=False)
    monkeypatch.setattr(
        "kiro_crew.session_pid.ancestry_inspection_supported", lambda *a, **kw: True
    )
    monkeypatch.setattr("kiro_crew.session_pid.agent_marker_in_ancestry", lambda *a, **kw: False)


@pytest.fixture(autouse=True)
def _secrets_write_stubbed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub _secrets_gateway_write -> no-op for tests that don't test gateway routing.

    Mutating verbs (``set``/``rm``) route writes through the live gateway, so
    unit tests that don't specifically test gateway routing stub the write helper
    to avoid needing a running gateway.

    Tests that specifically exercise the gateway-write path opt out with
    ``@pytest.mark.real_gateway_write``.
    """
    if request.node.get_closest_marker("real_gateway_write"):
        return
    monkeypatch.setattr("kiro_crew.cli._secrets_gateway_write", lambda *a, **kw: None)


class TestSecretsAgentGate:
    """The agent env-var gate blocks secrets in spawned sessions."""

    def test_blocked_in_agent_session(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """A secrets subcommand exits 1 when KIROCREW_SPAWNED is set.

        ALL verbs (list/set/rm/import) are gated in an agent session — see
        ``test_list_blocked_in_agent_session``; ``list`` reads the owner-only
        vault so even name disclosure is denied.
        """
        from kiro_crew.cli import _secrets

        monkeypatch.setenv("KIROCREW_SPAWNED", "1")

        class Args:
            secrets_action = "set"
            name = "API_KEY"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _secrets(Args())

        assert exc_info.value.code == 1

    def test_list_blocked_in_agent_session(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """``list`` is DENIED in an agent session.

        ``list`` reads the encrypted owner-only vault via
        ``SecretVault.list_names()``, so disclosing secret NAMES to an agent
        session leaks the identifiers of protected secrets. The verb stays
        available to a human in their own terminal.
        """
        from kiro_crew.cli import _secrets

        monkeypatch.setenv("KIROCREW_SPAWNED", "1")

        class Args:
            secrets_action = "list"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _secrets(Args())

        assert exc_info.value.code == 1

    def test_blocked_even_with_empty_value(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """Gate fires on key presence, not truthiness — empty string still blocks."""
        from kiro_crew.cli import _secrets

        monkeypatch.setenv("KIROCREW_SPAWNED", "")

        class Args:
            secrets_action = "set"
            name = "API_KEY"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _secrets(Args())

        assert exc_info.value.code == 1

    def test_allowed_without_env(self, vault_dir: Path, capsys) -> None:
        """secrets subcommand works when not in an agent session."""
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "API_KEY" in out

    def test_blocked_via_ancestry_when_env_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """A MUTATING verb is refused when an ANCESTOR carries the marker even
        though this process's own ``KIROCREW_SPAWNED`` is unset.

        This is the escalation GPT flagged: an agent ``env -u
        KIROCREW_SPAWNED``-es (so the env check fails open) then runs ``kirocrew
        secrets set``. The ancestry walk still sees the gateway/ACP marker in an
        ancestor's environ, which the child cannot rewrite, so the write is
        refused before ``.local_secret`` is ever read.
        """
        from kiro_crew.cli import _secrets

        # env var explicitly absent (the scrub); the gate still blocks because a
        # marked ancestor is found.
        monkeypatch.delenv("KIROCREW_SPAWNED", raising=False)
        monkeypatch.setattr(
            "kiro_crew.session_pid.agent_session_blocks_vault_mutation", lambda *a, **kw: True
        )

        class Args:
            secrets_action = "rm"
            name = "API_KEY"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _secrets(Args())

        assert exc_info.value.code == 1

    def test_list_blocked_when_gate_blocks(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """``list`` consults the SAME agent-session gate as the mutating verbs,
        so it is denied whenever the gate blocks (env marker, marked ancestor,
        or unsupported-platform fail-closed)."""
        from kiro_crew.cli import _secrets

        monkeypatch.setattr(
            "kiro_crew.session_pid.agent_session_blocks_vault_mutation", lambda *a, **kw: True
        )

        class Args:
            secrets_action = "list"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _secrets(Args())

        assert exc_info.value.code == 1


class TestSecretsGatewayWrite:
    """_secrets_gateway_write routes mutations through the gateway, fail-closed."""

    @pytest.mark.real_gateway_write
    def test_gateway_down_exits_cleanly(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        """When the gateway is not running, set/rm exits 1 with a clear message."""
        from kiro_crew import cli

        monkeypatch.setattr("kiro_crew.config.loader.read_local_secret", lambda port: "")

        class _FakeCfg:
            class dashboard:
                url = "http://127.0.0.1:5476"

        monkeypatch.setattr(
            cli, "KiroCrewConfig", type("C", (), {"load": staticmethod(lambda: _FakeCfg())})
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.urls.parse_dashboard_url",
            lambda url: ("127.0.0.1", 5476),
        )

        with pytest.raises(SystemExit) as exc_info:
            cli._secrets_gateway_write("set", name="K", value="v")

        assert exc_info.value.code == 1
        assert "gateway" in capsys.readouterr().err.lower()

    @pytest.mark.real_gateway_write
    def test_truncated_success_response_exits_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, capsys, tmp_path
    ) -> None:
        """A gateway restart mid-200 truncates the body → clean exit(1), not a traceback.

        `json.loads(resp.read())` on a partial body raises JSONDecodeError; the
        handler must catch it and exit cleanly with an 'outcome unknown' message.
        """
        import json

        import kiro_crew.loopback_http as _lh
        from kiro_crew import cli

        monkeypatch.setattr("kiro_crew.config.loader.read_local_secret", lambda port: "tok")

        class _FakeCfg:
            class dashboard:
                url = "http://127.0.0.1:5476"

        monkeypatch.setattr(
            cli, "KiroCrewConfig", type("C", (), {"load": staticmethod(lambda: _FakeCfg())})
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.urls.parse_dashboard_url",
            lambda url: ("127.0.0.1", 5476),
        )

        token_body = json.dumps({"token": "mytoken123"}).encode()
        call_count = [0]

        class _FakeResp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        def _fake_open(req, timeout=5):
            call_count[0] += 1
            if call_count[0] == 1:
                return _FakeResp(token_body)
            # The write response: a truncated/partial JSON body (gateway went
            # down mid-send) → json.loads raises inside the handler.
            return _FakeResp(b'{"ok": tr')

        monkeypatch.setattr(_lh, "loopback_urlopen", _fake_open)

        with pytest.raises(SystemExit) as exc_info:
            cli._secrets_gateway_write("set", name="K", value="v")

        assert exc_info.value.code == 1
        err = capsys.readouterr().err.lower()
        assert "unreadable" in err or "unknown" in err

    def _capture_sel(self, monkeypatch: pytest.MonkeyPatch, cli) -> list[dict]:
        """Install a SEL stub that records every log_tool_invocation call.

        The lambda is wrapped in ``staticmethod`` because a plain lambda placed
        in a class body via ``type()`` becomes a BOUND method and would receive
        ``self`` as its first argument.
        """
        calls: list[dict] = []
        monkeypatch.setattr(
            cli,
            "sel",
            lambda: type(
                "S", (), {"log_tool_invocation": staticmethod(lambda **kw: calls.append(kw))}
            )(),
        )
        return calls

    @pytest.mark.real_gateway_write
    def test_agent_denial_is_audited_with_a_machine_readable_reason(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """The agent env-marker denial emits a SEL event with reason=agent_env_marker."""
        from kiro_crew import cli

        monkeypatch.setenv("KIROCREW_SPAWNED", "1")
        calls = self._capture_sel(monkeypatch, cli)

        class Args:
            secrets_action = "rm"
            name = "API_KEY"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit),
        ):
            cli._secrets(Args())

        assert calls, "denial emitted no SEL event"
        assert calls[0]["outcome"] == "denied"
        assert calls[0]["metadata"]["reason"] == "agent_env_marker"
        # The deny path never records a secret name: the caller was not
        # authorized to name one.
        assert "API_KEY" not in repr(calls)

    def test_env_marker_denial_is_audited(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        from kiro_crew import cli

        monkeypatch.setenv("KIROCREW_SPAWNED", "1")
        calls = self._capture_sel(monkeypatch, cli)

        class Args:
            secrets_action = "set"
            name = "API_KEY"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit),
        ):
            cli._secrets(Args())

        assert calls[0]["outcome"] == "denied"
        assert calls[0]["metadata"]["reason"] == "agent_env_marker"

    def test_set_mutation_is_audited_without_the_value(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
    ) -> None:
        """The audit records the NAME only — never the secret value."""
        from kiro_crew import cli

        monkeypatch.setattr("sys.stdin", io.StringIO("super-secret-value\n"))
        calls = self._capture_sel(monkeypatch, cli)

        class Args:
            secrets_action = "set"
            name = "AUDITED"
            stdin = True

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            cli._secrets(Args())

        stored = [c for c in calls if c["outcome"] == "stored"]
        assert stored, "successful set emitted no SEL event"
        assert stored[0]["metadata"] == {"name": "AUDITED"}
        # The value must appear nowhere in the audit payload.
        assert "super-secret-value" not in repr(calls)

    def test_rm_mutation_is_audited(self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path) -> None:
        from kiro_crew import cli

        calls = self._capture_sel(monkeypatch, cli)

        class Args:
            secrets_action = "rm"
            name = "API_KEY"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            cli._secrets(Args())

        deleted = [c for c in calls if c["outcome"] == "deleted"]
        assert deleted, "successful rm emitted no SEL event"
        assert deleted[0]["metadata"] == {"name": "API_KEY"}

    @pytest.mark.real_gateway_write
    def test_gateway_write_succeeds_no_cap_header(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """set/rm reach the gateway without sending X-Vault-Write-Cap."""
        import json

        import kiro_crew.loopback_http as _lh
        from kiro_crew import cli

        monkeypatch.setattr("kiro_crew.config.loader.read_local_secret", lambda port: "tok")

        class _FakeCfg:
            class dashboard:
                url = "http://127.0.0.1:5476"

        monkeypatch.setattr(
            cli, "KiroCrewConfig", type("C", (), {"load": staticmethod(lambda: _FakeCfg())})
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.urls.parse_dashboard_url",
            lambda url: ("127.0.0.1", 5476),
        )

        sent_headers: list[dict] = []
        call_count = [0]

        class _FakeResp:
            def __init__(self, data):
                self._data = json.dumps(data).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        def _fake_open(req, timeout=5):
            call_count[0] += 1
            sent_headers.append(dict(req.headers))
            if call_count[0] == 1:
                return _FakeResp({"token": "tok123"})
            return _FakeResp({"ok": True, "name": "K"})

        monkeypatch.setattr(_lh, "loopback_urlopen", _fake_open)

        cli._secrets_gateway_write("set", name="K", value="v")

        assert call_count[0] == 2, "expected token mint + actual write"
        # Verify NO cap header was sent on the write request.
        write_headers = sent_headers[1]
        assert (
            "X-vault-write-cap" not in write_headers
        ), "X-Vault-Write-Cap must NOT be sent after descope"


class TestSecretsListCommand:
    """Tests for `kirocrew secrets list`."""

    def test_lists_secret_names(self, vault_dir: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "API_KEY" in out
        assert "DB_PASS" in out

    def test_list_reports_corrupt_vault_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, capsys
    ) -> None:
        """A restored/corrupt `secrets.enc` makes list_names() raise; `list`
        must surface a clean error with a nonzero exit, never a traceback."""
        from kiro_crew.cli import _secrets

        def _boom(self):
            raise ValueError("secrets.enc: Expecting value: line 1 column 1")

        monkeypatch.setattr(SecretVault, "list_names", _boom)

        class Args:
            secrets_action = "list"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _secrets(Args())

        assert exc_info.value.code == 1
        assert "error:" in capsys.readouterr().err.lower()

    def test_escape_control_chars_neutralizes_terminal_sequences(self) -> None:
        """C0/C1/ESC/CSI/OSC bytes are rendered inert; printable text survives."""
        from kiro_crew.cli import _escape_control_chars

        # OSC window-title set + BEL, and a CSI clear-screen.
        raw = "evil\x1b]0;pwned\x07\x1b[2Jname"
        escaped = _escape_control_chars(raw)
        assert "\x1b" not in escaped
        assert "\x07" not in escaped
        assert "\\x1b" in escaped and "\\x07" in escaped
        # A lone C1 CSI introducer (single byte) is escaped too.
        assert _escape_control_chars("x\x9by") == "x\\x9by"
        # Ordinary names (incl. Unicode) pass through unchanged.
        assert _escape_control_chars("API_KEY-\u00e9") == "API_KEY-\u00e9"

    def test_list_escapes_control_chars_in_names(self, tmp_path: Path, capsys) -> None:
        """`secrets list` prints an escaped form of a control-char-laden name."""
        from kiro_crew.cli import _secrets

        vault = SecretVault(tmp_path)
        vault._set_sync("evil\x1b]0;pwned\x07name", "v")

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x07" not in out
        assert "\\x1b" in out

    def test_empty_vault(self, empty_vault_dir: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "list"

        with patch("kiro_crew.cli.config_dir", return_value=str(empty_vault_dir)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "No secrets stored" in out


class TestSecretsSetCommand:
    """Tests for `kirocrew secrets set`."""

    def test_set_with_stdin_flag(self, tmp_path: Path, capsys) -> None:
        import io

        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "set"
            name = "NEW_SECRET"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("sys.stdin", io.StringIO("my-value-123\n")),
        ):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "stored" in out.lower()

        # Verify that the gateway-write helper was called (the actual write is
        # the gateway's responsibility; _secrets_gateway_write is stubbed to
        # no-op by the autouse fixture so we just verify the output).
        # Value-preservation (no newline stripping) is tested in
        # test_set_passes_value_verbatim_to_gateway below.

    def test_set_stdin_without_trailing_newline_stored_verbatim(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A piped value with no trailing newline is stored exactly as given."""
        import io

        from kiro_crew.cli import _secrets

        monkeypatch.setattr("sys.stdin", io.StringIO("no-newline-secret"))

        class Args:
            secrets_action = "set"
            name = "NEW_SECRET"
            stdin = True

        with patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)):
            _secrets(Args())

        # The CLI passes the raw value (no stripping) to _secrets_gateway_write.
        # _secrets_gateway_write is stubbed to no-op; verify output indicates success.
        # (The gateway integration test verifies the gateway handler receives it verbatim.)

    def test_list_reports_structurally_corrupt_vault_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, capsys
    ) -> None:
        """A structurally-broken envelope (entries: null / non-object root) makes
        list_names raise TypeError/AttributeError, not just ValueError; the CLI
        must still surface a clean error, never a traceback."""
        from kiro_crew.cli import _secrets

        def _boom(self):
            raise TypeError("'NoneType' object is not iterable")

        monkeypatch.setattr(SecretVault, "list_names", _boom)

        class Args:
            secrets_action = "list"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _secrets(Args())

        assert exc_info.value.code == 1
        assert "error:" in capsys.readouterr().err.lower()

    def test_set_rejects_non_utf8_value_with_clean_error(self, tmp_path: Path, capsys) -> None:
        """A value piped from a non-UTF-8 source (surrogate escapes survive the
        stdin decode) must fail with a clean CLI error and a non-zero exit, and
        must NOT write a partial/garbled secret."""
        import io

        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "set"
            name = "BINARY"
            stdin = True

        # A lone surrogate is exactly what reaches us after Python decodes a
        # non-UTF-8 pipe with surrogateescape; it cannot be re-encoded as UTF-8.
        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("sys.stdin", io.StringIO("bad-\udcff-value\n")),
            pytest.raises(SystemExit) as exc,
        ):
            _secrets(Args())

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "not valid utf-8" in err.lower()
        # Nothing was stored.
        assert SecretVault(tmp_path).get("BINARY") is None

    def test_set_rejects_non_utf8_name_with_clean_error(self, tmp_path: Path, capsys) -> None:
        """A secret NAME carrying an undecodable POSIX-argv byte (a surrogate)
        must fail with a clean CLI error and non-zero exit, not crash inside
        vault.set at AAD/key encoding, and must store nothing."""
        import io

        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "set"
            name = "BAD-\udcff-NAME"  # lone surrogate: not UTF-8 encodable
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("sys.stdin", io.StringIO("some-value\n")),
            pytest.raises(SystemExit) as exc,
        ):
            _secrets(Args())

        assert exc.value.code != 0
        assert "name is not valid utf-8" in capsys.readouterr().err.lower()

    def test_set_stdin_read_decode_error_is_clean(self, tmp_path: Path, capsys) -> None:
        """If the text-mode stdin raises UnicodeDecodeError at read() (a pipe of
        raw non-UTF-8 bytes under strict decoding), `set` must emit the clean
        invalid-UTF-8 error and exit non-zero, not surface a traceback."""
        from kiro_crew.cli import _secrets

        class _BadStdin:
            def read(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        class Args:
            secrets_action = "set"
            name = "BINARY"
            stdin = True

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("sys.stdin", _BadStdin()),
            pytest.raises(SystemExit) as exc,
        ):
            _secrets(Args())

        assert exc.value.code != 0
        assert "not valid utf-8" in capsys.readouterr().err.lower()
        assert SecretVault(tmp_path).get("BINARY") is None
        """`rm` has the same non-UTF-8 name hazard as `set` (the name reaches
        vault.delete AAD/key encoding): reject cleanly before the vault call."""
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "rm"
            name = "BAD-\udcff-NAME"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            pytest.raises(SystemExit) as exc,
        ):
            _secrets(Args())

        assert exc.value.code != 0
        assert "name is not valid utf-8" in capsys.readouterr().err.lower()

    def test_set_prompts_for_value(self, tmp_path: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "set"
            name = "PROMPTED"
            stdin = False

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("getpass.getpass", return_value="prompted-value"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            _secrets(Args())

        # _secrets_gateway_write is stubbed to no-op; the success output confirms
        # the CLI reached the write call with the prompted value.

    def test_set_escapes_control_chars_in_prompt_and_confirmation(
        self, tmp_path: Path, capsys
    ) -> None:
        """A control-char name is escaped in BOTH the getpass prompt and the
        stored-confirmation line — a dashboard-set name must not be able to
        rewrite the operator's terminal when echoed."""
        from kiro_crew.cli import _secrets

        raw = "EVIL\x1b]0;pwn\x07KEY"

        class Args:
            secrets_action = "set"
            name = raw
            stdin = False

        captured_prompt = {}

        def _fake_getpass(prompt: str = "") -> str:
            captured_prompt["p"] = prompt
            return "prompted-value"

        with (
            patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)),
            patch("getpass.getpass", _fake_getpass),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            _secrets(Args())

        # Prompt: escaped form present, raw ESC/BEL bytes gone.
        assert "\\x1b" in captured_prompt["p"]
        assert "\x1b" not in captured_prompt["p"]
        assert "\x07" not in captured_prompt["p"]

        # Confirmation line: same guarantee.
        out = capsys.readouterr().out
        assert "\\x1b" in out
        assert "\x1b" not in out
        assert "\x07" not in out

        # The RAW name was still passed to the write helper (escaping is display-only).
        # _secrets_gateway_write is stubbed to no-op in unit tests.


class TestSecretsRmCommand:
    """Tests for `kirocrew secrets rm`."""

    def test_rm_existing_secret(self, vault_dir: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "rm"
            name = "API_KEY"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "deleted" in out.lower()

        # _secrets_gateway_write is stubbed to no-op in unit tests — just
        # verify the CLI reached the delete call and printed success.

    def test_rm_escapes_control_chars_in_confirmation(self, tmp_path: Path, capsys) -> None:
        """The rm confirmation escapes a control-char name (display-only)."""
        from kiro_crew.cli import _secrets

        raw = "EVIL\x1b[2JKEY"
        # Use _set_sync (the synchronous write path) instead of asyncio.run(vault.set(...)).
        # asyncio.run() on Windows creates and immediately closes a ProactorEventLoop
        # whose ThreadPoolExecutor spawns a thread that opens subprocess pipe handles
        # via icacls (restrict_to_owner).  Those IOCP handles remain registered after
        # loop.close(), corrupting subsequent subprocess.run pipe communication on the
        # same xdist worker process -- causing stdout_thread.join() to deadlock and
        # crashing the whole worker.  _set_sync is the identical code path without
        # any event-loop machinery, so the test is equivalent and Windows-safe.
        SecretVault(tmp_path)._set_sync(raw, "v")

        class Args:
            secrets_action = "rm"
            name = raw

        with patch("kiro_crew.cli.config_dir", return_value=str(tmp_path)):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "deleted" in out.lower()
        assert "\\x1b" in out
        assert "\x1b" not in out
        # The RAW name is passed to the write helper (escaping is display-only).
        # _secrets_gateway_write is stubbed to no-op in unit tests.

    def test_rm_nonexistent_is_noop(self, vault_dir: Path, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = "rm"
            name = "MISSING"

        with patch("kiro_crew.cli.config_dir", return_value=str(vault_dir)):
            _secrets(Args())

        # Should not error
        out = capsys.readouterr().out
        assert "deleted" in out.lower()


class TestSecretsNoAction:
    """Tests for `kirocrew secrets` with no subcommand."""

    def test_shows_usage(self, capsys) -> None:
        from kiro_crew.cli import _secrets

        class Args:
            secrets_action = None

        with patch("kiro_crew.cli.config_dir", return_value="/tmp"):
            _secrets(Args())

        out = capsys.readouterr().out
        assert "Usage" in out or "usage" in out.lower()


class TestConfigSandboxGatewayRoute:
    """``kirocrew config set agent.sandbox`` must route through the gateway.

    A direct disk write cannot tear down live agent sessions; the gateway PATCH
    handler does that under a single lock with fail-closed revert.  When the
    gateway is not running the command MUST exit nonzero and leave config.json
    untouched.
    """

    def test_agent_sandbox_no_gateway_exits_nonzero_and_no_config_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """``config set agent.sandbox auto`` without a running gateway exits 1
        and does NOT modify config.json."""
        import argparse

        from kiro_crew.cli_config import _config_cmd

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"agent": {"sandbox": "off"}}', encoding="utf-8")

        monkeypatch.setattr("kiro_crew.cli_config.config_path", lambda: cfg_file)
        monkeypatch.setattr(
            "kiro_crew.cli_config.config_local_path", lambda: tmp_path / "config.local.json"
        )

        # Simulate no running gateway: read_local_secret returns None.
        monkeypatch.setattr(
            "kiro_crew.cli_config.KiroCrewConfig.load",
            lambda: _MockCfgForGatewayRoute(),
        )

        # _config_sandbox_via_gateway is called; it calls read_local_secret.
        # We patch loopback_urlopen and read_local_secret inside the cli_config
        # module's imported namespace.

        def _no_secret(_port):
            return None  # simulates gateway not running

        monkeypatch.setattr("kiro_crew.config.loader.read_local_secret", _no_secret)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=False,
            file=None,
        )

        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)

        assert exc.value.code != 0, "must exit nonzero when gateway is not running"

        # config.json must not have been modified.
        contents = cfg_file.read_text(encoding="utf-8")
        import json

        assert (
            json.loads(contents)["agent"]["sandbox"] == "off"
        ), "config.json must remain unchanged when gateway is not running"

        err = capsys.readouterr().err
        assert "gateway" in err.lower(), "error message must mention the gateway"

    def test_agent_sandbox_local_is_refused_and_no_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """``config set --local agent.sandbox auto`` is refused: a local overlay
        also cannot tear down live agent sessions, so it must not write the
        overlay directly. Exits nonzero and writes NO config.local.json."""
        import argparse

        from kiro_crew.cli_config import _config_cmd

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"agent": {"sandbox": "off"}}', encoding="utf-8")
        local_file = tmp_path / "config.local.json"
        monkeypatch.setattr("kiro_crew.cli_config.config_path", lambda: cfg_file)
        monkeypatch.setattr("kiro_crew.cli_config.config_local_path", lambda: local_file)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=True,
            file=None,
        )
        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)
        assert exc.value.code != 0, "must exit nonzero for --local agent.sandbox"
        assert not local_file.exists(), "no config.local.json should be written"
        err = capsys.readouterr().err
        assert "--local" in err, "error must explain --local is refused"

    # ------------------------------------------------------------------
    # Gateway-present paths for _config_sandbox_via_gateway
    # ------------------------------------------------------------------

    def _setup_gateway_route(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """Patch the minimal set of symbols needed by _config_sandbox_via_gateway.

        Returns a config.json path that MUST remain unwritten after the call.
        """
        from kiro_crew.cli_config import _config_cmd  # noqa: F401 (import side-effect)

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"agent": {"sandbox": "off"}}', encoding="utf-8")

        monkeypatch.setattr("kiro_crew.cli_config.config_path", lambda: cfg_file)
        monkeypatch.setattr(
            "kiro_crew.cli_config.config_local_path",
            lambda: tmp_path / "config.local.json",
        )
        monkeypatch.setattr(
            "kiro_crew.cli_config.KiroCrewConfig.load",
            lambda: _MockCfgForGatewayRoute(),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.urls.parse_dashboard_url",
            lambda url: ("127.0.0.1", 6188),
        )
        # Stub sel() so log_api_access does not try to reach a real SEL backend.
        monkeypatch.setattr(
            "kiro_crew.cli_config.sel",
            lambda: type(
                "_SelStub",
                (),
                {"log_api_access": staticmethod(lambda **kw: None)},
            )(),
        )
        return cfg_file

    def test_success_path_no_config_write_and_prints_ok(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """token mint + PATCH both succeed -> prints success, config.json untouched."""
        import argparse
        import json

        from kiro_crew.cli_config import _config_cmd

        cfg_file = self._setup_gateway_route(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_local_secret",
            lambda port: "secret-abc",
        )

        token_body = json.dumps({"token": "BEARER"}).encode()
        patch_body = json.dumps({"ok": True}).encode()
        call_count = [0]

        class _FakeResp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *_: object) -> None:
                pass

        def _fake_open(req: object, timeout: int = 5) -> _FakeResp:
            call_count[0] += 1
            if call_count[0] == 1:
                return _FakeResp(token_body)
            return _FakeResp(patch_body)

        monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", _fake_open)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=False,
            file=None,
        )
        _config_cmd(args)  # must not raise

        out = capsys.readouterr().out
        assert "agent.sandbox" in out, "success message must mention the key"
        assert call_count[0] == 2, "loopback_urlopen must be called twice"
        # config.json must NOT be written directly by _config_cmd
        assert (
            json.loads(cfg_file.read_text(encoding="utf-8"))["agent"]["sandbox"] == "off"
        ), "config.json must remain unchanged; gateway owns the write"

    def test_patch_error_body_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """PATCH returns {\"error\": \"nope\"} -> SystemExit(1), message includes error."""
        import argparse
        import json

        from kiro_crew.cli_config import _config_cmd

        self._setup_gateway_route(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_local_secret",
            lambda port: "secret-abc",
        )

        token_body = json.dumps({"token": "BEARER"}).encode()
        error_body = json.dumps({"error": "nope", "code": "some_code"}).encode()
        call_count = [0]

        class _FakeResp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *_: object) -> None:
                pass

        def _fake_open(req: object, timeout: int = 5) -> _FakeResp:
            call_count[0] += 1
            if call_count[0] == 1:
                return _FakeResp(token_body)
            return _FakeResp(error_body)

        monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", _fake_open)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=False,
            file=None,
        )
        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "nope" in err, "error message must include the server error text"

    def test_http_error_on_patch_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """HTTPError on PATCH -> SystemExit(1) with server error in message."""
        import argparse
        import json
        import urllib.error

        from kiro_crew.cli_config import _config_cmd

        self._setup_gateway_route(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_local_secret",
            lambda port: "secret-abc",
        )

        token_body = json.dumps({"token": "BEARER"}).encode()
        http_err_body = json.dumps({"error": "bad", "code": "x"}).encode()
        call_count = [0]

        class _FakeResp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *_: object) -> None:
                pass

        def _fake_open(req: object, timeout: int = 5) -> _FakeResp:
            call_count[0] += 1
            if call_count[0] == 1:
                return _FakeResp(token_body)
            raise urllib.error.HTTPError(
                url="http://127.0.0.1:6188/api/config/kirocrew",
                code=500,
                msg="Internal Server Error",
                hdrs=None,  # type: ignore[arg-type]
                fp=type(
                    "_FP",
                    (),
                    {"read": lambda self: http_err_body},
                )(),
            )

        monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", _fake_open)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="off",
            local=False,
            file=None,
        )
        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "bad" in err, "error from HTTPError body must appear in message"
        assert "x" in err, "code from HTTPError body must appear in message"

    def test_url_error_on_token_mint_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """URLError on token-mint call -> SystemExit(1) with 'could not connect'."""
        import argparse
        import urllib.error

        from kiro_crew.cli_config import _config_cmd

        self._setup_gateway_route(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_local_secret",
            lambda port: "secret-abc",
        )

        def _fake_open(req: object, timeout: int = 5) -> object:
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", _fake_open)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=False,
            file=None,
        )
        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "could not connect" in err.lower(), "error must mention 'could not connect'"

    def test_empty_bearer_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Token-mint response with no 'token' key -> SystemExit(1), no token message."""
        import argparse
        import json

        from kiro_crew.cli_config import _config_cmd

        self._setup_gateway_route(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_local_secret",
            lambda port: "secret-abc",
        )

        empty_token_body = json.dumps({}).encode()  # no "token" key

        class _FakeResp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *_: object) -> None:
                pass

        def _fake_open(req: object, timeout: int = 5) -> _FakeResp:
            return _FakeResp(empty_token_body)

        monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", _fake_open)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=False,
            file=None,
        )
        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "token" in err.lower(), "error must mention token when bearer is missing"

    def test_generic_exception_on_token_mint_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Non-URLError exception during token mint -> bearer becomes '' -> SystemExit(1)."""
        import argparse

        from kiro_crew.cli_config import _config_cmd

        self._setup_gateway_route(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_local_secret",
            lambda port: "secret-abc",
        )

        class _FakeResp:
            def read(self) -> bytes:
                raise ValueError("unexpected parse error")

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *_: object) -> None:
                pass

        def _fake_open(req: object, timeout: int = 5) -> _FakeResp:
            return _FakeResp()

        monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", _fake_open)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=False,
            file=None,
        )
        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert (
            "token" in err.lower()
        ), "error must mention token when bearer is empty after generic exception"

    def test_http_error_on_patch_non_json_body_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """HTTPError on PATCH whose body is not JSON -> fallback message with HTTP code."""
        import argparse
        import json
        import urllib.error

        from kiro_crew.cli_config import _config_cmd

        self._setup_gateway_route(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_local_secret",
            lambda port: "secret-abc",
        )

        token_body = json.dumps({"token": "BEARER"}).encode()
        call_count = [0]

        class _FakeResp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *_: object) -> None:
                pass

        def _fake_open(req: object, timeout: int = 5) -> _FakeResp:
            call_count[0] += 1
            if call_count[0] == 1:
                return _FakeResp(token_body)
            raise urllib.error.HTTPError(
                url="http://127.0.0.1:6188/api/config/kirocrew",
                code=502,
                msg="Bad Gateway",
                hdrs=None,  # type: ignore[arg-type]
                fp=type(
                    "_FP",
                    (),
                    {"read": lambda self: b"not json at all"},
                )(),
            )

        monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", _fake_open)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=False,
            file=None,
        )
        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "502" in err, "fallback message must include the HTTP status code"

    def test_url_error_on_patch_call_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """URLError raised on the PATCH call (not the token mint) -> SystemExit(1)."""
        import argparse
        import json
        import urllib.error

        from kiro_crew.cli_config import _config_cmd

        self._setup_gateway_route(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_local_secret",
            lambda port: "secret-abc",
        )

        token_body = json.dumps({"token": "BEARER"}).encode()
        call_count = [0]

        class _FakeResp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *_: object) -> None:
                pass

        def _fake_open(req: object, timeout: int = 5) -> _FakeResp:
            call_count[0] += 1
            if call_count[0] == 1:
                return _FakeResp(token_body)
            raise urllib.error.URLError("Connection reset by peer")

        monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", _fake_open)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=False,
            file=None,
        )
        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert (
            "could not connect" in err.lower()
        ), "URLError on PATCH must produce 'could not connect' message"

    def test_http_error_on_patch_no_code_key_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """HTTPError on PATCH with JSON body that has no 'code' -> message without parens."""
        import argparse
        import json
        import urllib.error

        from kiro_crew.cli_config import _config_cmd

        self._setup_gateway_route(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_local_secret",
            lambda port: "secret-abc",
        )

        token_body = json.dumps({"token": "BEARER"}).encode()
        # error body has error but no "code" key -> the ``if code:`` branch is skipped
        no_code_body = json.dumps({"error": "some error"}).encode()
        call_count = [0]

        class _FakeResp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *_: object) -> None:
                pass

        def _fake_open(req: object, timeout: int = 5) -> _FakeResp:
            call_count[0] += 1
            if call_count[0] == 1:
                return _FakeResp(token_body)
            raise urllib.error.HTTPError(
                url="http://127.0.0.1:6188/api/config/kirocrew",
                code=400,
                msg="Bad Request",
                hdrs=None,  # type: ignore[arg-type]
                fp=type(
                    "_FP",
                    (),
                    {"read": lambda self: no_code_body},
                )(),
            )

        monkeypatch.setattr("kiro_crew.loopback_http.loopback_urlopen", _fake_open)

        args = argparse.Namespace(
            config_action="set",
            key="agent.sandbox",
            value="auto",
            local=False,
            file=None,
        )
        with pytest.raises(SystemExit) as exc:
            _config_cmd(args)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "some error" in err, "error text must appear in message"
        assert "(" not in err or ")" not in err, "no code key means no parenthesised code in output"


class _MockCfgForGatewayRoute:
    """Minimal stand-in for KiroCrewConfig for the gateway-route test."""

    class dashboard:
        url = "http://127.0.0.1:6188"

    class agent:
        sandbox = "off"

    def to_dict(self):
        return {"agent": {"sandbox": "off"}}


# ── Finding B tests: --file import agent.sandbox gate ─────────────────────────


class TestConfigSetFileAgentSandboxGate:
    """``config set --file`` must route agent.sandbox changes through the gateway.

    A direct disk write cannot tear down live agent sessions or rotate the
    vault-write capability, so a ``--file`` import that changes ``agent.sandbox``
    must delegate the sandbox portion to ``_config_sandbox_via_gateway`` (the
    PATCH path with fail-closed teardown) and only write the remaining keys
    directly.
    """

    def _make_current_config(self, sandbox_value: str):
        """Return a minimal fake KiroCrewConfig-like object."""
        return type(
            "_Cfg",
            (),
            {
                "agent": type("_A", (), {"sandbox": sandbox_value})(),
                "dashboard": type("_D", (), {"url": "http://127.0.0.1:7500"})(),
            },
        )()

    def test_file_import_with_sandbox_change_routes_via_gateway(
        self, tmp_path: Path, capsys
    ) -> None:
        """--file import with agent.sandbox off→auto calls _config_sandbox_via_gateway."""
        from unittest.mock import MagicMock, patch

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        import_file = tmp_path / "import.json"
        import_file.write_text(
            '{"agent": {"sandbox": "auto", "model": "claude-opus"}}', encoding="utf-8"
        )

        gateway_calls: list = []
        written_data_list: list = []

        def _fake_gateway(value):
            gateway_calls.append(value)

        def _capture_write(path, mutate, **kw):
            # Call the mutate to capture what data was passed for writing.
            result = mutate({})
            written_data_list.append(result)
            return result

        with (
            patch("kiro_crew.cli_config.config_path", return_value=cfg_path),
            patch(
                "kiro_crew.cli_config.KiroCrewConfig.load",
                return_value=self._make_current_config("off"),
            ),
            patch("kiro_crew.cli_config._config_sandbox_via_gateway", side_effect=_fake_gateway),
            patch(
                "kiro_crew.cli_config.update_config_locked",
                side_effect=_capture_write,
            ),
            patch("kiro_crew.cli_config.sel", return_value=MagicMock()),
        ):
            from kiro_crew.cli_config import _config_cmd

            args = type(
                "_A",
                (),
                {"config_action": "set", "file": str(import_file), "key": None, "value": None},
            )()
            _config_cmd(args)

        # Gateway was called with "auto".
        assert gateway_calls == ["auto"], "sandbox change must be routed through gateway"
        # Direct write must NOT include agent.sandbox in the written data.
        for written in written_data_list:
            agent_section = written.get("agent", {})
            assert (
                "sandbox" not in agent_section
            ), f"sandbox key must be stripped from direct write; got agent={agent_section}"

    def test_file_import_with_same_sandbox_value_does_not_call_gateway(
        self, tmp_path: Path, capsys
    ) -> None:
        """--file import where agent.sandbox matches current value does NOT call gateway."""
        from unittest.mock import MagicMock, patch

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        import_file = tmp_path / "import.json"
        import_file.write_text('{"agent": {"sandbox": "auto"}}', encoding="utf-8")

        gateway_calls: list = []

        def _fake_gateway(value):
            gateway_calls.append(value)

        with (
            patch("kiro_crew.cli_config.config_path", return_value=cfg_path),
            patch(
                "kiro_crew.cli_config.KiroCrewConfig.load",
                return_value=self._make_current_config("auto"),
            ),
            patch("kiro_crew.cli_config._config_sandbox_via_gateway", side_effect=_fake_gateway),
            patch(
                "kiro_crew.cli_config.update_config_locked",
                side_effect=lambda path, mutate, **kw: mutate({}),
            ),
            patch("kiro_crew.cli_config.sel", return_value=MagicMock()),
        ):
            from kiro_crew.cli_config import _config_cmd

            args = type(
                "_A",
                (),
                {"config_action": "set", "file": str(import_file), "key": None, "value": None},
            )()
            _config_cmd(args)

        assert gateway_calls == [], "no gateway call when sandbox value unchanged"

    def test_file_import_without_sandbox_key_does_not_call_gateway(
        self, tmp_path: Path, capsys
    ) -> None:
        """F1a: --file import without agent.sandbox key AND on-disk 'off' MUST call
        the gateway with effective default 'auto'.

        Before the fix, an absent key was treated as 'no change' and the on-disk
        'off' value silently survived.  After the fix, the effective default for an
        absent key is 'auto', so the off->auto transition is routed through the
        gateway teardown path.
        """
        from unittest.mock import MagicMock, patch

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        import_file = tmp_path / "import.json"
        import_file.write_text('{"dashboard": {"bot_name": "test"}}', encoding="utf-8")

        gateway_calls: list = []

        with (
            patch("kiro_crew.cli_config.config_path", return_value=cfg_path),
            patch(
                "kiro_crew.cli_config.KiroCrewConfig.load",
                return_value=self._make_current_config("off"),
            ),
            patch(
                "kiro_crew.cli_config._config_sandbox_via_gateway",
                side_effect=lambda v: gateway_calls.append(v),
            ),
            patch(
                "kiro_crew.cli_config.update_config_locked",
                side_effect=lambda path, mutate, **kw: mutate({}),
            ),
            patch("kiro_crew.cli_config.sel", return_value=MagicMock()),
        ):
            from kiro_crew.cli_config import _config_cmd

            args = type(
                "_A",
                (),
                {"config_action": "set", "file": str(import_file), "key": None, "value": None},
            )()
            _config_cmd(args)

        assert gateway_calls == ["auto"], (
            f"Expected gateway called with 'auto' (off->effective-default-auto); "
            f"got {gateway_calls!r}"
        )

    def test_file_import_sandbox_change_preserves_gateway_applied_sandbox_in_persisted_config(
        self, tmp_path: Path
    ) -> None:
        """F1: after --file import with sandbox change, persisted config still carries
        the gateway-applied agent.sandbox value.

        The whole-file replacement `mutate=lambda _: data` previously dropped
        agent.sandbox because the sandbox key was stripped from `data` before the
        gateway call.  The fix uses a mutate(old) that copies the current on-disk
        sandbox back into the replacement data so the key is never lost.
        """
        from unittest.mock import MagicMock, patch

        cfg_path = tmp_path / "config.json"
        # Seed an existing config that already has agent.sandbox="off" on disk.
        initial = {"agent": {"sandbox": "off", "model": "claude-opus"}, "dashboard": {}}
        cfg_path.write_text(__import__("json").dumps(initial), encoding="utf-8")

        import_file = tmp_path / "import.json"
        # The import file wants sandbox="auto" AND adds an unrelated key.
        import_file.write_text(
            __import__("json").dumps(
                {"agent": {"sandbox": "auto", "model": "claude-haiku"}, "dashboard": {}}
            ),
            encoding="utf-8",
        )

        # Simulate the gateway PATCH applying "auto" to disk.
        def _fake_gateway(value):
            current = __import__("json").loads(cfg_path.read_text(encoding="utf-8"))
            current.setdefault("agent", {})["sandbox"] = value
            cfg_path.write_text(__import__("json").dumps(current), encoding="utf-8")

        # Use the real update_config_locked so the mutate closure runs against
        # the actual on-disk file (which now has sandbox="auto" from the gateway).
        from kiro_crew.config.loader import update_config_locked

        with (
            patch("kiro_crew.cli_config.config_path", return_value=cfg_path),
            patch(
                "kiro_crew.cli_config.KiroCrewConfig.load",
                return_value=self._make_current_config("off"),
            ),
            patch("kiro_crew.cli_config._config_sandbox_via_gateway", side_effect=_fake_gateway),
            patch(
                "kiro_crew.cli_config.update_config_locked",
                side_effect=lambda path, mutate, **kw: update_config_locked(
                    path, mutate=mutate, **kw
                ),
            ),
            patch("kiro_crew.cli_config.sel", return_value=MagicMock()),
        ):
            from kiro_crew.cli_config import _config_cmd

            args = type(
                "_A",
                (),
                {"config_action": "set", "file": str(import_file), "key": None, "value": None},
            )()
            _config_cmd(args)

        persisted = __import__("json").loads(cfg_path.read_text(encoding="utf-8"))
        agent_section = persisted.get("agent", {})
        assert agent_section.get("sandbox") == "auto", (
            f"persisted config must retain the gateway-applied sandbox='auto'; "
            f"got agent={agent_section}"
        )
        # Unrelated keys must also survive.
        assert (
            agent_section.get("model") == "claude-haiku"
        ), "unrelated agent keys must be preserved in the merged write"


class TestImportApplyFloorGate:
    """``secrets import --apply`` is gated by the vault floor posture."""

    def _make_args(self, apply: bool):
        return type(
            "_A",
            (),
            {"secrets_action": "import", "apply": apply},
        )()

    def test_import_dry_run_bypasses_floor_check(self, tmp_path: Path) -> None:
        """``import`` (dry-run, no --apply) never checks floor posture."""
        from kiro_crew import cli_commands

        # Even with ABSENT posture, dry_run should succeed (no vault writes).
        with (
            patch("kiro_crew.sandbox.vault_floor_posture", return_value="absent"),
            patch(
                "kiro_crew.cli_commands.migrate_env_secrets",
                return_value=type("_R", (), {"migrated": [], "skipped": [], "conflicts": []})(),
            ),
            patch("kiro_crew.cli_commands.format_report", return_value="dry-run OK"),
        ):
            # Should NOT raise SystemExit — dry run bypasses the floor gate.
            args = self._make_args(apply=False)
            # We patch _handle_secrets via cli_commands directly.
            cli_commands._handle_secrets(args)

    def test_import_apply_absent_posture_is_refused(self, tmp_path: Path, capsys) -> None:
        """``import --apply`` with ABSENT posture exits 1 with a clear error."""
        from kiro_crew import cli_commands
        from kiro_crew.sandbox import VAULT_FLOOR_ABSENT

        with (
            patch("kiro_crew.sandbox.configured_sandbox_mode", return_value="auto"),
            patch("kiro_crew.sandbox.vault_floor_posture", return_value=VAULT_FLOOR_ABSENT),
            patch(
                "kiro_crew.cli_commands.sel",
                return_value=type("_FS", (), {"log_tool_invocation": lambda self, **kw: None})(),
            ),
        ):
            args = self._make_args(apply=True)
            with pytest.raises(SystemExit) as exc:
                cli_commands._handle_secrets(args)
            assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "vault_floor" in captured.err or "sandbox floor" in captured.err

    def test_import_apply_enforced_posture_proceeds(self, tmp_path: Path) -> None:
        """``import --apply`` with ENFORCED posture proceeds to migration."""
        from kiro_crew import cli_commands
        from kiro_crew.sandbox import VAULT_FLOOR_ENFORCED

        called = []

        def _fake_migrate(dry_run):
            called.append(dry_run)
            return type("_R", (), {"migrated": [], "skipped": [], "conflicts": []})()

        with (
            patch("kiro_crew.sandbox.configured_sandbox_mode", return_value="auto"),
            patch("kiro_crew.sandbox.vault_floor_posture", return_value=VAULT_FLOOR_ENFORCED),
            patch("kiro_crew.cli_commands.migrate_env_secrets", side_effect=_fake_migrate),
            patch("kiro_crew.cli_commands.format_report", return_value="OK"),
        ):
            args = self._make_args(apply=True)
            cli_commands._handle_secrets(args)

        assert called == [False], "migrate_env_secrets should be called with dry_run=False"

    def test_import_apply_not_applicable_posture_proceeds(self, tmp_path: Path) -> None:
        """``import --apply`` on a platform with no vault-hide mechanism succeeds."""
        from kiro_crew import cli_commands
        from kiro_crew.sandbox import VAULT_FLOOR_NOT_APPLICABLE

        called = []

        def _fake_migrate(dry_run):
            called.append(dry_run)
            return type("_R", (), {"migrated": [], "skipped": [], "conflicts": []})()

        with (
            patch("kiro_crew.sandbox.configured_sandbox_mode", return_value="auto"),
            patch("kiro_crew.sandbox.vault_floor_posture", return_value=VAULT_FLOOR_NOT_APPLICABLE),
            patch("kiro_crew.cli_commands.migrate_env_secrets", side_effect=_fake_migrate),
            patch("kiro_crew.cli_commands.format_report", return_value="OK"),
        ):
            args = self._make_args(apply=True)
            cli_commands._handle_secrets(args)

        assert called == [False], "migrate_env_secrets should be called with dry_run=False"
