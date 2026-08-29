"""Tests for sandbox.vault_floor_posture — the three-way vault floor decision.

Matrix:
  mechanism-absent (permanent no-backend) -> NOT_APPLICABLE / allow
  mechanism-present + masking vault       -> ENFORCED / allow
  sandbox=off on capable host             -> ABSENT / refuse
  vault relocated outside hide set        -> ABSENT / refuse
  transient probe failure                 -> ABSENT / refuse (fail-closed)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kiro_crew import sandbox
from kiro_crew.sandbox import (
    VAULT_FLOOR_ABSENT,
    VAULT_FLOOR_ENFORCED,
    VAULT_FLOOR_NOT_APPLICABLE,
    vault_floor_posture,
)


def _reset():
    """Reset cached backend + last failure so tests start from a clean slate."""
    sandbox.reset_backend()


class TestVaultFloorPostureMatrix:
    """Core three-way matrix tests for vault_floor_posture()."""

    def test_mechanism_absent_permanent_is_not_applicable(self) -> None:
        """When the platform has NO vault-hide mechanism (permanent probe failure)
        AND agents cannot run unconfined, posture is NOT_APPLICABLE (owner gate allows)."""
        _reset()
        # Simulate a permanent no-backend host: detect_backend returns "none"
        # and _last_unshare_failure indicates non-transient failure. No unsandboxed
        # exec opt-in, so no unconfined agent can exist to read the vault.
        with (
            patch.object(sandbox, "detect_backend", return_value="none"),
            patch.object(sandbox, "_last_unshare_failure", (False, "EINVAL no CONFIG_USER_NS", "")),
            patch.object(sandbox, "_allow_unsandboxed_exec", return_value=False),
            patch.object(sandbox, "_allow_no_isolation", return_value=False),
        ):
            posture = vault_floor_posture("auto")
        assert posture == VAULT_FLOOR_NOT_APPLICABLE

    def test_no_mechanism_but_unsandboxed_exec_enabled_is_absent(self) -> None:
        """No-backend host WITH agent.sandbox_allow_unsandboxed_exec: an unconfined
        agent can import SecretVault and read the vault, so posture is ABSENT (refuse)."""
        _reset()
        with (
            patch.object(sandbox, "detect_backend", return_value="none"),
            patch.object(sandbox, "_last_unshare_failure", (False, "EINVAL no CONFIG_USER_NS", "")),
            patch.object(sandbox, "_allow_unsandboxed_exec", return_value=True),
            patch.object(sandbox, "_allow_no_isolation", return_value=False),
        ):
            posture = vault_floor_posture("auto")
        assert posture == VAULT_FLOOR_ABSENT

    def test_no_mechanism_but_no_isolation_enabled_is_absent(self) -> None:
        """No-backend host WITH agent.sandbox_allow_no_isolation: same exposure,
        posture is ABSENT (refuse)."""
        _reset()
        with (
            patch.object(sandbox, "detect_backend", return_value="none"),
            patch.object(sandbox, "_last_unshare_failure", (False, "EINVAL no CONFIG_USER_NS", "")),
            patch.object(sandbox, "_allow_unsandboxed_exec", return_value=False),
            patch.object(sandbox, "_allow_no_isolation", return_value=True),
        ):
            posture = vault_floor_posture("auto")
        assert posture == VAULT_FLOOR_ABSENT

    def test_sandbox_off_no_mechanism_but_unsandboxed_exec_is_absent(self) -> None:
        """sandbox=off on a no-mechanism host with unsandboxed exec enabled: ABSENT."""
        _reset()
        with (
            patch.object(sandbox, "detect_backend", return_value="none"),
            patch.object(sandbox, "_allow_unsandboxed_exec", return_value=True),
            patch.object(sandbox, "_allow_no_isolation", return_value=False),
        ):
            posture = vault_floor_posture("off")
        assert posture == VAULT_FLOOR_ABSENT

    def test_mechanism_present_and_masking_is_enforced(self, tmp_path: Path) -> None:
        """When a backend exists AND the resolved vault dir is inside the hide set,
        posture is ENFORCED."""
        _reset()
        # Vault at the default $HOME-relative path (covered by hide entries).
        home = Path.home()
        with (
            patch.object(sandbox, "detect_backend", return_value="namespace"),
            patch("kiro_crew.sandbox.config_dir", return_value=home / ".kiro" / "crew"),
        ):
            posture = vault_floor_posture("auto")
        assert posture == VAULT_FLOOR_ENFORCED

    def test_sandbox_off_on_capable_host_is_absent(self) -> None:
        """sandbox=off on a namespace-capable host: posture is ABSENT (refuse)."""
        _reset()
        # configured_mode="off" → detect_backend("off")=none, but capable_backend=namespace
        with patch.object(sandbox, "detect_backend") as mock_detect:

            def _detect(mode):
                if mode == "off":
                    return "none"
                return "namespace"

            mock_detect.side_effect = _detect
            posture = vault_floor_posture("off")
        assert posture == VAULT_FLOOR_ABSENT

    def test_vault_relocated_outside_hide_set_is_absent(self, tmp_path: Path) -> None:
        """When KIROCREW_HOME places the vault outside every hide-list entry,
        posture is ABSENT even though a backend exists."""
        _reset()
        # Vault at a custom location (e.g. /srv/crew/.vault) not under $HOME.
        custom_config = tmp_path / "crew"
        custom_config.mkdir()
        with (
            patch.object(sandbox, "detect_backend", return_value="namespace"),
            # config_dir() returns a path that is NOT under $HOME
            patch("kiro_crew.sandbox.config_dir", return_value=custom_config),
        ):
            posture = vault_floor_posture("auto")
        # The vault at tmp_path/crew/.vault is not inside ~/.kiro/crew/.vault or
        # ~/.kirocrew/.vault → ABSENT.
        assert posture == VAULT_FLOOR_ABSENT

    def test_transient_probe_failure_is_absent(self) -> None:
        """A transient backend failure is treated as ABSENT (fail-closed).

        A momentary fork exhaustion must not grant a write window.
        """
        _reset()
        with (
            patch.object(sandbox, "detect_backend", return_value="none"),
            patch.object(sandbox, "_last_unshare_failure", (True, "EAGAIN transient", "")),
        ):
            posture = vault_floor_posture("auto")
        assert posture == VAULT_FLOOR_ABSENT

    def test_sandbox_off_no_capable_host_is_absent(self) -> None:
        """sandbox=off is a deliberate opt-out of isolation: even on a host with
        no hide mechanism, `off` lets an unconfined agent run and read the vault,
        so posture is ABSENT (refuse), NOT NOT_APPLICABLE. (GPT 5.6 finding: a
        raw agent spawn under off can decrypt the vault, so writes must refuse.)"""
        _reset()
        with patch.object(sandbox, "detect_backend", return_value="none"):
            # Both "off" and "auto" return "none" → no mechanism at all, but the
            # operator chose off, so an unconfined agent is possible.
            posture = vault_floor_posture("off")
        assert posture == VAULT_FLOOR_ABSENT


class TestVaultFloorInForceBooleanWrapper:
    """vault_floor_in_force() is a backwards-compatible bool wrapper."""

    def test_enforced_returns_true(self) -> None:
        _reset()
        with (patch("kiro_crew.sandbox.vault_floor_posture", return_value=VAULT_FLOOR_ENFORCED),):
            assert sandbox.vault_floor_in_force("auto") is True

    def test_not_applicable_returns_true(self) -> None:
        _reset()
        with (
            patch("kiro_crew.sandbox.vault_floor_posture", return_value=VAULT_FLOOR_NOT_APPLICABLE),
        ):
            assert sandbox.vault_floor_in_force("auto") is True

    def test_absent_returns_false(self) -> None:
        _reset()
        with (patch("kiro_crew.sandbox.vault_floor_posture", return_value=VAULT_FLOOR_ABSENT),):
            assert sandbox.vault_floor_in_force("auto") is False


class TestVaultDirIsHidden:
    """Unit tests for _vault_dir_is_hidden()."""

    def test_default_home_vault_is_hidden(self) -> None:
        """The default vault at ~/.kiro/crew/.vault is reported as hidden."""
        home = Path.home()
        with patch("kiro_crew.sandbox.config_dir", return_value=home / ".kiro" / "crew"):
            assert sandbox._vault_dir_is_hidden("namespace") is True

    def test_relocated_vault_is_not_hidden(self, tmp_path: Path) -> None:
        """A vault relocated outside $HOME is NOT considered hidden."""
        custom = tmp_path / "srv" / "crew"
        custom.mkdir(parents=True)
        with patch("kiro_crew.sandbox.config_dir", return_value=custom):
            assert sandbox._vault_dir_is_hidden("namespace") is False

    def test_legacy_kirocrew_vault_is_hidden(self) -> None:
        """The legacy ~/.kirocrew/.vault path is also reported as hidden."""
        home = Path.home()
        with patch("kiro_crew.sandbox.config_dir", return_value=home / ".kirocrew"):
            # config_dir() / ".vault" = ~/.kirocrew/.vault which IS in the hide list
            assert sandbox._vault_dir_is_hidden("namespace") is True
