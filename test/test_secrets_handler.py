"""Tests for kiro_crew.dashboard.handlers.secrets."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers.secrets import setup_secrets_routes
from kiro_crew.secrets import SecretVault


class _FakeState:
    """No owner configured: only the signed local bootstrap subjects pass."""

    owner_id = ""


def _app() -> web.Application:
    """A dashboard app whose secrets routes see an AUTHENTICATED owner caller.

    Stands in for ``token_auth_middleware``: the secrets handlers gate on
    ``is_owner_dashboard_request``, which reads ``request["user"]`` /
    ``request["app"]`` and ``app["state"].owner_id``. With no owner configured,
    the default local-app subject (empty app + ``local-app`` user) is the
    implicit owner, so existing behavioural tests exercise the authorized path.
    A test can select a different caller via the ``X-Test-User`` /
    ``X-Test-App`` headers.

    ``vault_floor_in_force`` is set to ``True`` so the server-side boot-posture
    gate passes transparently in all existing tests; use ``_app_no_floor()``
    to exercise the gate's deny path.
    """

    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    app["vault_floor_in_force"] = True
    setup_secrets_routes(app)
    return app


def _app_no_floor() -> web.Application:
    """Like ``_app()`` but with ``vault_floor_in_force=False`` to exercise the deny path."""

    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    app["vault_floor_in_force"] = False
    setup_secrets_routes(app)
    return app


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    """Create a vault with test data."""
    vault = SecretVault(tmp_path)
    vault._set_sync("TEST_KEY", "test-value-123")
    vault._set_sync("DB_PASS", "hunter2")
    return tmp_path


@pytest.fixture()
def empty_vault_dir(tmp_path: Path) -> Path:
    return tmp_path


class TestApiSecretsList:
    """Tests for GET /api/secrets."""

    @pytest.mark.asyncio
    async def test_lists_names_sorted(self, vault_dir: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                data = await resp.json()
                assert data == {"names": ["DB_PASS", "TEST_KEY"]}

    @pytest.mark.asyncio
    async def test_empty_vault(self, empty_vault_dir: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(empty_vault_dir)
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                data = await resp.json()
                assert data == {"names": []}


class TestApiSecretsSet:
    """Tests for POST /api/secrets."""

    @pytest.mark.asyncio
    async def test_stores_secret(self, tmp_path: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/secrets",
                    json={"name": "NEW_KEY", "value": "new-value"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True

                # Verify stored
                vault = SecretVault(tmp_path)
                assert vault.get("NEW_KEY").reveal() == "new-value"

    @pytest.mark.asyncio
    async def test_missing_name(self, tmp_path: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"value": "x"})
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_value(self, tmp_path: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "X"})
                assert resp.status == 400


class TestApiSecretsDelete:
    """Tests for DELETE /api/secrets/{name}."""

    @pytest.mark.asyncio
    async def test_deletes_secret(self, vault_dir: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/TEST_KEY")
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True

                vault = SecretVault(vault_dir)
                assert vault.get("TEST_KEY") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, vault_dir: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/MISSING")
                assert resp.status == 200  # delete is idempotent


class TestApiSecretsOwnerAuthorization:
    """Every /api/secrets route is owner-only (CWE-862).

    The AES-256-GCM vault is machine-global keystone-floor material, so an app
    token or any authenticated non-owner dashboard subject must not enumerate,
    overwrite/poison, or delete entries. The handlers gate on
    ``is_owner_dashboard_request`` — an app-scoped caller (non-empty ``app``)
    and a non-owner user are both refused with 403 ``owner_only``, and nothing
    is written.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path", "json_body"),
        [
            ("get", "/api/secrets", None),
            ("post", "/api/secrets", {"name": "EVIL", "value": "x"}),
            ("delete", "/api/secrets/TEST_KEY", None),
        ],
    )
    async def test_app_token_is_refused(
        self, vault_dir: Path, method: str, path: str, json_body: object
    ) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                # X-Test-App non-empty => an app-scoped (non-owner) caller.
                headers = {"X-Test-App": "some-app", "X-Test-User": "some-app-subject"}
                resp = await getattr(client, method)(path, headers=headers, json=json_body)
                assert resp.status == 403
                data = await resp.json()
                assert data["code"] == "owner_only"
                # The write surfaces must not have mutated the vault.
                assert sorted(SecretVault(vault_dir).list_names()) == ["DB_PASS", "TEST_KEY"]

    @pytest.mark.asyncio
    async def test_non_owner_user_is_refused_when_owner_configured(self, vault_dir: Path) -> None:
        app = _app()
        app["state"].owner_id = "U_OWNER"  # type: ignore[attr-defined]

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                headers = {"X-Test-User": "U_SOMEONE_ELSE"}
                resp = await client.post(
                    "/api/secrets", headers=headers, json={"name": "EVIL", "value": "x"}
                )
                assert resp.status == 403
                data = await resp.json()
                assert data["code"] == "owner_only"
                assert sorted(SecretVault(vault_dir).list_names()) == ["DB_PASS", "TEST_KEY"]

    @pytest.mark.asyncio
    async def test_denial_is_audited(self, vault_dir: Path) -> None:
        """A non-owner denial writes a SEL audit record (outcome="denied"), so a
        rejected attempt on the vault is not silent — matching the sibling
        agent-spec / aws-consent / messaging handlers."""
        app = _app()

        from unittest.mock import MagicMock

        from aiohttp.test_utils import TestClient, TestServer

        sel = MagicMock()
        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)),
            patch("kiro_crew.dashboard.handlers.secrets._sel", return_value=sel),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/secrets",
                    headers={"X-Test-App": "some-app", "X-Test-User": "some-app-subject"},
                    json={"name": "EVIL", "value": "x"},
                )
                assert resp.status == 403
        sel.log_api_access.assert_called_once()
        kwargs = sel.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["operation"] == "secrets_set"
        assert kwargs["source"] == "dashboard"

    @pytest.mark.asyncio
    async def test_configured_owner_is_allowed(self, vault_dir: Path) -> None:
        app = _app()
        app["state"].owner_id = "U_OWNER"  # type: ignore[attr-defined]

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets", headers={"X-Test-User": "U_OWNER"})
                assert resp.status == 200


class TestApiSecretsLogInjection:
    """Control characters in the secret name must not reach the log verbatim (CWE-117).

    ``name`` is free-form user input trimmed only with ``.strip()``, which
    leaves interior ``\\n`` / ``\\r`` in place. Unsanitized, that forges extra
    log lines / fake audit entries. The handler escapes control characters
    before logging, so the emitted record stays on one line.
    """

    @pytest.mark.asyncio
    async def test_newline_in_set_name_is_escaped_in_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = _app()
        payload = "ok\nWARNING forged audit line"

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.handlers.secrets"):
                    resp = await client.post(
                        "/api/secrets",
                        json={"name": payload, "value": "v"},
                    )
                    assert resp.status == 200

        stored = [r for r in caplog.records if "stored via dashboard" in r.getMessage()]
        assert stored, "expected a 'stored via dashboard' log record"
        msg = stored[0].getMessage()
        # The raw newline must not survive into the log line.
        assert "\n" not in msg
        assert "\\n" in msg

    @pytest.mark.asyncio
    async def test_crlf_in_delete_name_is_escaped_in_log(
        self, vault_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        # A percent-encoded CR/LF in the path segment decodes to real control
        # characters in request.match_info["name"].
        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.handlers.secrets"):
                    resp = await client.delete("/api/secrets/x%0d%0aWARNING-forged")
                    assert resp.status == 200

        deleted = [r for r in caplog.records if "deleted via dashboard" in r.getMessage()]
        assert deleted, "expected a 'deleted via dashboard' log record"
        msg = deleted[0].getMessage()
        assert "\n" not in msg
        assert "\r" not in msg


class TestApiSecretsSetInputValidation:
    """POST /api/secrets rejects well-formed JSON of the wrong shape with 400.

    These bodies all parse as valid JSON, so they get past the JSONDecodeError
    guard. Before the type checks, `body.get("name", "").strip()` raised
    AttributeError on each of them and surfaced as an HTTP 500.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ([{"name": "A", "value": "b"}], "invalid_body"),  # JSON array
            ("just a string", "invalid_body"),  # JSON string
            (42, "invalid_body"),  # JSON number
            ({"name": 123, "value": "b"}, "invalid_name_type"),  # non-string name
            ({"name": ["A"], "value": "b"}, "invalid_name_type"),  # list name
            ({"name": None, "value": "b"}, "invalid_name_type"),  # null name
            ({"value": "b"}, "invalid_name_type"),  # name absent entirely
            ({"name": "A", "value": 123}, "invalid_value_type"),  # non-string value
            ({"name": "A", "value": {"k": "v"}}, "invalid_value_type"),  # dict value
            ({"name": "A"}, "invalid_value_type"),  # value absent entirely
        ],
    )
    async def test_rejects_wrong_types_with_400(
        self, empty_vault_dir: Path, body: object, code: str
    ) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json=body)
                assert resp.status == 400
                data = await resp.json()
                assert data["code"] == code
                # Nothing was written to the vault on a rejected request.
                assert SecretVault(empty_vault_dir).list_names() == []

    @pytest.mark.asyncio
    async def test_accepts_valid_string_payload(self, empty_vault_dir: Path) -> None:
        """The happy path still works after the added type checks."""
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "  PADDED  ", "value": "v"})
                assert resp.status == 200
                data = await resp.json()
                # Name is still trimmed, as before.
                assert data["name"] == "PADDED"
                assert SecretVault(empty_vault_dir).list_names() == ["PADDED"]


class TestApiSecretsOwnerWriteSucceeds:
    """Owner-authorized set/delete work without any cap or floor gate."""

    @pytest.mark.asyncio
    async def test_owner_set_succeeds_no_cap(self, tmp_path: Path) -> None:
        """POST /api/secrets by the owner succeeds — no cap or floor header needed."""
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/secrets",
                    json={"name": "OWNER_KEY", "value": "owner-value"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert SecretVault(tmp_path).get("OWNER_KEY") is not None

    @pytest.mark.asyncio
    async def test_owner_delete_succeeds_no_cap(self, vault_dir: Path) -> None:
        """DELETE /api/secrets/{name} by the owner succeeds — no cap or floor header needed."""
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/TEST_KEY")
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert SecretVault(vault_dir).get("TEST_KEY") is None


# ── F2 lock-atomicity tests ────────────────────────────────────────────────────


class TestSecretsLockAtomicity:
    """The mutation path acquires _get_config_lock before vault read/write.

    A concurrent agent.sandbox PATCH rotates config under the same lock.
    Without the lock, a concurrent config rotation could overlap with a vault
    write, introducing a race condition.
    """

    @pytest.mark.asyncio
    async def test_set_acquires_config_lock(self, tmp_path: Path) -> None:
        """POST /api/secrets acquires _get_config_lock before vault write."""
        import asyncio
        from unittest.mock import patch

        lock_acquired_during_vault_write = []

        real_lock = __import__(
            "kiro_crew.dashboard.handlers.agents", fromlist=["_get_config_lock"]
        )._get_config_lock()

        original_lock_aenter = real_lock.__aenter__

        lock_is_held = asyncio.Event()

        class _TrackingLock:
            async def __aenter__(self_inner):
                lock_is_held.set()
                return await original_lock_aenter()

            async def __aexit__(self_inner, *args):
                lock_is_held.clear()
                return await real_lock.__aexit__(*args)

        class _FakeVault:
            async def set(self, name, value):
                # Record whether the lock was held when vault.set was called.
                lock_acquired_during_vault_write.append(lock_is_held.is_set())

        app = _app()

        with (
            patch(
                "kiro_crew.dashboard.handlers.secrets._get_config_lock",
                return_value=_TrackingLock(),
            ),
            patch(
                "kiro_crew.dashboard.handlers.secrets.SecretVault",
                return_value=_FakeVault(),
            ),
        ):
            from aiohttp.test_utils import TestClient, TestServer

            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/secrets",
                    json={"name": "MY_KEY", "value": "s3cret"},
                )

        assert resp.status == 200
        assert lock_acquired_during_vault_write, "vault.set was never called"
        assert (
            lock_acquired_during_vault_write[0] is True
        ), "lock must be held when vault.set is called"

    @pytest.mark.asyncio
    async def test_delete_acquires_config_lock(self, tmp_path: Path) -> None:
        """DELETE /api/secrets/{name} acquires _get_config_lock before vault write."""
        import asyncio
        from unittest.mock import patch

        lock_acquired_during_vault_delete = []

        real_lock = __import__(
            "kiro_crew.dashboard.handlers.agents", fromlist=["_get_config_lock"]
        )._get_config_lock()

        original_lock_aenter = real_lock.__aenter__

        lock_is_held = asyncio.Event()

        class _TrackingLock:
            async def __aenter__(self_inner):
                lock_is_held.set()
                return await original_lock_aenter()

            async def __aexit__(self_inner, *args):
                lock_is_held.clear()
                return await real_lock.__aexit__(*args)

        class _FakeVault:
            async def delete(self, name):
                lock_acquired_during_vault_delete.append(lock_is_held.is_set())

        app = _app()

        with (
            patch(
                "kiro_crew.dashboard.handlers.secrets._get_config_lock",
                return_value=_TrackingLock(),
            ),
            patch(
                "kiro_crew.dashboard.handlers.secrets.SecretVault",
                return_value=_FakeVault(),
            ),
        ):
            from aiohttp.test_utils import TestClient, TestServer

            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/MY_KEY")

        assert resp.status == 200
        assert lock_acquired_during_vault_delete, "vault.delete was never called"
        assert (
            lock_acquired_during_vault_delete[0] is True
        ), "lock must be held when vault.delete is called"


class TestVaultFloorGate:
    """Server-side boot-posture gate: set/delete require vault_floor_in_force=True."""

    @pytest.mark.asyncio
    async def test_set_succeeds_when_floor_in_force(self, tmp_path: Path) -> None:
        """POST /api/secrets succeeds when vault_floor_in_force is True (normal path)."""
        app = _app()  # vault_floor_in_force=True

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "MY_KEY", "value": "s3cret"})
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_set_denied_when_floor_not_in_force(self, tmp_path: Path) -> None:
        """POST /api/secrets returns 403 vault_floor_not_in_force when floor absent."""
        app = _app_no_floor()  # vault_floor_in_force=False

        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets._sel",
                return_value=type(
                    "_FakeSel",
                    (),
                    {"log_api_access": lambda self, **kw: None},
                )(),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "MY_KEY", "value": "s3cret"})
                assert resp.status == 403
                data = await resp.json()
                assert data["code"] == "vault_floor_not_in_force"

    @pytest.mark.asyncio
    async def test_delete_denied_when_floor_not_in_force(self, tmp_path: Path) -> None:
        """DELETE /api/secrets/{name} returns 403 vault_floor_not_in_force when floor absent."""
        app = _app_no_floor()  # vault_floor_in_force=False

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets._sel",
            return_value=type(
                "_FakeSel",
                (),
                {"log_api_access": lambda self, **kw: None},
            )(),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/MY_KEY")
                assert resp.status == 403
                data = await resp.json()
                assert data["code"] == "vault_floor_not_in_force"

    @pytest.mark.asyncio
    async def test_list_unaffected_by_floor(self, vault_dir: Path) -> None:
        """GET /api/secrets (list) is NOT gated by vault_floor_in_force."""
        app = _app_no_floor()  # vault_floor_in_force=False, but list should still work

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                data = await resp.json()
                # List returns the names regardless of floor posture
                assert "names" in data


def _app_posture(posture: str) -> web.Application:
    """Like ``_app()`` but with an explicit ``vault_floor_posture`` string.

    Used to exercise the three-way (ENFORCED / ABSENT / NOT_APPLICABLE) paths
    introduced by the posture redesign.  The legacy ``vault_floor_in_force``
    boolean is derived from the posture string to keep backwards compatibility.
    """
    from kiro_crew.sandbox import VAULT_FLOOR_ABSENT

    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    app["vault_floor_posture"] = posture
    app["vault_floor_in_force"] = posture != VAULT_FLOOR_ABSENT
    setup_secrets_routes(app)
    return app


class TestVaultFloorThreeWayPosture:
    """Three-way posture gate: ENFORCED and NOT_APPLICABLE allow; ABSENT refuses."""

    @pytest.mark.asyncio
    async def test_enforced_allows_set(self, tmp_path: Path) -> None:
        """ENFORCED posture: POST /api/secrets succeeds."""
        from kiro_crew.sandbox import VAULT_FLOOR_ENFORCED

        app = _app_posture(VAULT_FLOOR_ENFORCED)
        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "K", "value": "v"})
                assert resp.status == 200
                assert (await resp.json())["ok"] is True

    @pytest.mark.asyncio
    async def test_not_applicable_allows_set(self, tmp_path: Path) -> None:
        """NOT_APPLICABLE posture (Windows/no-userns): POST /api/secrets succeeds.

        On a platform with no vault-hide mechanism at all (Windows, no-userns)
        the owner gate is the only boundary; we must NOT 403 the owner's own
        Settings page.
        """
        from kiro_crew.sandbox import VAULT_FLOOR_NOT_APPLICABLE

        app = _app_posture(VAULT_FLOOR_NOT_APPLICABLE)
        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "K", "value": "v"})
                assert resp.status == 200
                assert (await resp.json())["ok"] is True

    @pytest.mark.asyncio
    async def test_absent_refuses_set(self, tmp_path: Path) -> None:
        """ABSENT posture: POST /api/secrets returns 403 vault_floor_not_in_force."""
        from kiro_crew.sandbox import VAULT_FLOOR_ABSENT

        app = _app_posture(VAULT_FLOOR_ABSENT)
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets._sel",
                return_value=type("_FakeSel", (), {"log_api_access": lambda self, **kw: None})(),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "K", "value": "v"})
                assert resp.status == 403
                assert (await resp.json())["code"] == "vault_floor_not_in_force"

    @pytest.mark.asyncio
    async def test_not_applicable_allows_delete(self, tmp_path: Path) -> None:
        """NOT_APPLICABLE posture: DELETE /api/secrets/{name} succeeds."""
        from kiro_crew.sandbox import VAULT_FLOOR_NOT_APPLICABLE

        vault = SecretVault(tmp_path)
        vault._set_sync("EXISTING", "val")
        app = _app_posture(VAULT_FLOOR_NOT_APPLICABLE)
        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/EXISTING")
                assert resp.status == 200

    @pytest.mark.asyncio
    async def test_absent_refuses_delete(self, tmp_path: Path) -> None:
        """ABSENT posture: DELETE /api/secrets/{name} returns 403."""
        from kiro_crew.sandbox import VAULT_FLOOR_ABSENT

        app = _app_posture(VAULT_FLOOR_ABSENT)
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets._sel",
            return_value=type("_FakeSel", (), {"log_api_access": lambda self, **kw: None})(),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/SOME_KEY")
                assert resp.status == 403
                assert (await resp.json())["code"] == "vault_floor_not_in_force"

    @pytest.mark.asyncio
    async def test_posture_absent_legacy_boolean_false(self, tmp_path: Path) -> None:
        """Legacy vault_floor_in_force=False (no posture key) still refuses mutations."""
        # This exercises the fallback path for test helpers using the old boolean.
        app = _app_no_floor()  # sets vault_floor_in_force=False, no vault_floor_posture key
        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets._sel",
                return_value=type("_FakeSel", (), {"log_api_access": lambda self, **kw: None})(),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "K", "value": "v"})
                assert resp.status == 403

    @pytest.mark.asyncio
    async def test_floor_check_runs_inside_lock_before_write(self, tmp_path: Path) -> None:
        """TOCTOU: posture flipped to ABSENT under the lock must refuse the write.

        The floor check now runs INSIDE _get_config_lock, immediately before the
        vault write. The agent.sandbox config-patch handler updates
        app['vault_floor_posture'] while holding that same lock. We simulate a
        concurrent sandbox-disable by flipping the posture to ABSENT at the moment
        the handler acquires the lock; the write must then be refused (403) rather
        than landing after isolation was removed. If the check still ran BEFORE the
        lock (the old TOCTOU), it would read the stale ENFORCED value and return 200.
        """
        from contextlib import asynccontextmanager

        from kiro_crew.sandbox import VAULT_FLOOR_ABSENT, VAULT_FLOOR_ENFORCED

        app = _app_posture(VAULT_FLOOR_ENFORCED)
        from aiohttp.test_utils import TestClient, TestServer

        @asynccontextmanager
        async def _flip_posture_lock():
            # Emulate a concurrent sandbox-disable that ran under the same lock:
            # by the time this handler holds the lock, the posture is ABSENT.
            app["vault_floor_posture"] = VAULT_FLOOR_ABSENT
            yield

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets._get_config_lock",
                _flip_posture_lock,
            ),
            patch(
                "kiro_crew.dashboard.handlers.secrets._sel",
                return_value=type("_FakeSel", (), {"log_api_access": lambda self, **kw: None})(),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "K", "value": "v"})
                assert resp.status == 403
                assert (await resp.json())["code"] == "vault_floor_not_in_force"
                from kiro_crew.secrets.vault import SecretVault

                assert SecretVault(str(tmp_path)).list_names() == []
