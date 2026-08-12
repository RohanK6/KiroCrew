"""Tests for authoring agent templates through the dashboard.

Covers ``POST /api/agents/installed`` and ``PUT /api/agents/installed/{name}``.

The load-bearing constraint behind most of these: kiro-cli validates
``~/.kiro/agents/*.json`` with serde ``deny_unknown_fields`` and rejects the
ENTIRE spec on any unknown key, then silently falls back to the default agent.
A spec written with a field kiro-cli does not know is not a degraded template —
it is a template that does not exist, while the session looks like it is running
the user's agent. Several tests below exist only to pin that.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.agent_discovery import clear_list_agents_cache
from kiro_crew.dashboard.handlers.agents import (
    api_agents_installed_create,
    api_agents_installed_update,
)

#: Keys kiro-cli accepts in an agent spec. Anything outside this set makes the
#: whole file unloadable, so a written spec must never contain one.
VALID_SPEC_KEYS = {
    "name",
    "description",
    "model",
    "prompt",
    "tools",
    "allowedTools",
    "mcpServers",
    "resources",
    "includeMcpJson",
    "hooks",
    "toolsSettings",
}


@pytest.fixture(autouse=True)
def _no_agent_cache():
    clear_list_agents_cache()
    yield
    clear_list_agents_cache()


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


def _request(method: str, body: object, name: str = "") -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.method = method
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}
    request.get = lambda key, default=None: default

    async def _json():
        if body is None:
            raise json.JSONDecodeError("no body", "", 0)
        return body

    request.json = _json
    return request


async def _create(agents_dir: Path, body: object) -> web.Response:
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        return await api_agents_installed_create(_request("POST", body))


async def _update(agents_dir: Path, name: str, body: object) -> web.Response:
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        return await api_agents_installed_update(_request("PUT", body, name=name))


def _body(resp: web.Response) -> dict:
    return json.loads(resp.body.decode("utf-8"))


def _written(agents_dir: Path, name: str) -> dict:
    return json.loads((agents_dir / f"{name}.json").read_text(encoding="utf-8"))


# ── The spec must only ever contain fields kiro-cli accepts ──


class TestSpecSchema:
    @pytest.mark.asyncio
    async def test_prompt_is_written_to_the_prompt_field(self, agents_dir):
        """`customInstructions` is not a kiro spec field. Writing it there makes
        deny_unknown_fields reject the whole spec, so a template with a prompt
        would silently not load at all."""
        await _create(agents_dir, {"name": "researcher", "prompt": "Be rigorous."})

        written = _written(agents_dir, "researcher")
        assert written["prompt"] == "Be rigorous."
        assert "customInstructions" not in written

    @pytest.mark.asyncio
    async def test_custom_instructions_alias_still_lands_in_prompt(self, agents_dir):
        """The alias is accepted from a request body for compatibility, but the
        spec key written is always `prompt`."""
        await _create(agents_dir, {"name": "researcher", "customInstructions": "Be terse."})

        written = _written(agents_dir, "researcher")
        assert written["prompt"] == "Be terse."
        assert "customInstructions" not in written

    @pytest.mark.asyncio
    async def test_no_unknown_key_reaches_the_spec(self, agents_dir):
        """A body full of plausible-but-invalid keys must not smuggle any of them
        into the file. One unknown key costs the entire template."""
        await _create(
            agents_dir,
            {
                "name": "researcher",
                "prompt": "hi",
                "customInstructions": "hi",
                "systemPrompt": "hi",
                "instructions": "hi",
                "temperature": 0.4,
                "maxTokens": 100,
                "model_managed": True,
                "cc_model": "x",
            },
        )

        assert set(_written(agents_dir, "researcher")) <= VALID_SPEC_KEYS

    @pytest.mark.asyncio
    async def test_denied_commands_is_refused(self, agents_dir):
        """Top-level it is an unknown key (whole spec rejected), and relocating it
        under toolsSettings would revive a retired mechanism that
        _strip_legacy_denied_commands deletes. Refused loudly, not dropped."""
        resp = await _create(
            agents_dir, {"name": "researcher", "deniedCommands": ["rm -rf /*"]}
        )

        assert resp.status == 400
        assert not (agents_dir / "researcher.json").exists()

    @pytest.mark.asyncio
    async def test_valid_privilege_fields_still_round_trip(self, agents_dir):
        """The scope this endpoint deliberately DOES grant must keep working."""
        await _create(
            agents_dir,
            {
                "name": "researcher",
                "tools": ["fs_read", "execute_bash"],
                "allowedTools": ["fs_read"],
                "resources": ["file://.kiro/steering/**/*.md"],
            },
        )

        written = _written(agents_dir, "researcher")
        assert written["tools"] == ["fs_read", "execute_bash"]
        assert written["allowedTools"] == ["fs_read"]
        assert written["resources"] == ["file://.kiro/steering/**/*.md"]


# ── Names ──


class TestNames:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["../escape", "sub/dir", "back\\slash", "..", ".hidden", "has space", "-lead"]
    )
    async def test_unsafe_names_are_rejected(self, agents_dir, name):
        resp = await _create(agents_dir, {"name": name})

        assert resp.status == 400
        assert list(agents_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_traversal_writes_nothing_outside_the_agents_dir(self, agents_dir, tmp_path):
        await _create(agents_dir, {"name": "../../pwned"})

        assert not (tmp_path / "pwned.json").exists()
        assert not (tmp_path.parent / "pwned.json").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["kirocrew", "kirocrew-lite", "kirocrew-knowledge", "default"]
    )
    async def test_managed_and_builtin_names_are_reserved(self, agents_dir, name):
        resp = await _create(agents_dir, {"name": name})

        assert resp.status == 400
        assert not (agents_dir / f"{name}.json").exists()

    @pytest.mark.asyncio
    async def test_a_name_claimed_by_a_package_spec_conflicts(self, agents_dir):
        """kiro-cli resolves by the `name` FIELD, so a free filename is not
        enough — two specs answering to one name is a coin flip."""
        (agents_dir / "somepkg-reviewer.json").write_text(
            '{"name": "reviewer"}', encoding="utf-8"
        )

        resp = await _create(agents_dir, {"name": "reviewer"})

        assert resp.status == 409
        assert not (agents_dir / "reviewer.json").exists()

    @pytest.mark.asyncio
    async def test_an_unparseable_neighbour_does_not_block_creation(self, agents_dir):
        (agents_dir / "broken.json").write_text("{not json", encoding="utf-8")

        resp = await _create(agents_dir, {"name": "researcher"})
        assert resp.status == 201


# ── Create is exclusive ──


class TestExclusiveCreate:
    @pytest.mark.asyncio
    async def test_existing_template_is_not_overwritten(self, agents_dir):
        (agents_dir / "researcher.json").write_text(
            '{"name": "researcher", "prompt": "original"}', encoding="utf-8"
        )

        resp = await _create(agents_dir, {"name": "researcher", "prompt": "replacement"})

        assert resp.status == 409
        assert _written(agents_dir, "researcher")["prompt"] == "original"

    @pytest.mark.asyncio
    async def test_a_file_appearing_after_the_scan_is_not_clobbered(self, agents_dir):
        """The TOCTOU window O_EXCL closes. Stubbing the scan to report a free
        name is exactly what the loser of a concurrent-create race sees, so this
        reaches the write with the file already present."""
        victim = agents_dir / "researcher.json"
        victim.write_text('{"name": "researcher", "prompt": "original"}', encoding="utf-8")

        with patch(
            "kiro_crew.dashboard.handlers.agents._name_already_claimed", return_value=False
        ):
            resp = await _create(agents_dir, {"name": "researcher", "prompt": "replacement"})

        assert resp.status == 409
        assert _written(agents_dir, "researcher")["prompt"] == "original"


# ── Update ──


class TestUpdate:
    @pytest.fixture
    def template(self, agents_dir) -> Path:
        path = agents_dir / "researcher.json"
        path.write_text(
            json.dumps({"name": "researcher", "description": "old", "prompt": "old"}),
            encoding="utf-8",
        )
        return path

    @pytest.mark.asyncio
    async def test_fields_are_replaced(self, agents_dir, template):
        resp = await _update(
            agents_dir, "researcher", {"description": "new", "prompt": "new prompt"}
        )

        assert resp.status == 200
        written = _written(agents_dir, "researcher")
        assert written["description"] == "new"
        assert written["prompt"] == "new prompt"

    @pytest.mark.asyncio
    async def test_unmodelled_keys_are_carried_forward(self, agents_dir):
        """The form builds a FRESH spec, so a plain full-replace would delete
        hand-authored keys it does not model — editing a description would drop
        the user's audit hook."""
        path = agents_dir / "researcher.json"
        path.write_text(
            json.dumps(
                {
                    "name": "researcher",
                    "description": "old",
                    "hooks": {"postToolUse": [{"matcher": "execute_bash", "command": "log"}]},
                    "includeMcpJson": False,
                    "toolsSettings": {"fs_write": {"someSetting": True}},
                }
            ),
            encoding="utf-8",
        )

        await _update(agents_dir, "researcher", {"description": "new"})

        written = _written(agents_dir, "researcher")
        assert written["description"] == "new"
        assert written["hooks"]["postToolUse"][0]["matcher"] == "execute_bash"
        assert written["includeMcpJson"] is False
        assert written["toolsSettings"] == {"fs_write": {"someSetting": True}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "filename",
        [
            "kirocrew.json",
            "kirocrew-lite.json",
            "kirocrew-knowledge.json",
            "kirocrew-research.json",
            "kirocrew-heartbeat.json",
        ],
    )
    async def test_kiro_crew_managed_specs_are_refused(self, agents_dir, filename):
        """None of these contains a double dash, so a `--`-only guard lets a PUT
        full-replace Kiro Crew's own agent — dropping hooks, includeMcpJson and
        the managed MCP block until the next install rebuild."""
        path = agents_dir / filename
        original = {
            "name": Path(filename).stem,
            "prompt": "file://managed.md",
            "hooks": {"postToolUse": [{"matcher": "execute_bash", "command": "audit"}]},
        }
        path.write_text(json.dumps(original), encoding="utf-8")

        resp = await _update(agents_dir, Path(filename).stem, {"description": "hijacked"})

        assert resp.status == 403
        assert json.loads(path.read_text(encoding="utf-8")) == original

    @pytest.mark.asyncio
    async def test_app_managed_specs_are_still_refused(self, agents_dir):
        path = agents_dir / "someapp--helper.json"
        path.write_text('{"name": "helper"}', encoding="utf-8")

        resp = await _update(agents_dir, "someapp--helper", {"description": "x"})
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_missing_template_is_a_404(self, agents_dir):
        resp = await _update(agents_dir, "nope", {"description": "x"})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_a_mismatched_body_name_is_rejected(self, agents_dir, template):
        resp = await _update(
            agents_dir, "researcher", {"name": "somethingelse", "description": "x"}
        )

        assert resp.status == 400
        assert _written(agents_dir, "researcher")["description"] == "old"

    @pytest.mark.asyncio
    async def test_denied_commands_is_refused_on_update_too(self, agents_dir, template):
        resp = await _update(agents_dir, "researcher", {"deniedCommands": ["rm -rf /*"]})

        assert resp.status == 400
        assert "deniedCommands" not in _written(agents_dir, "researcher")
