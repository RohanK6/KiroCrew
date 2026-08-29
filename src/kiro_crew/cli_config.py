"""CLI config subcommand — get, set, edit configuration values."""

from __future__ import annotations

import argparse
import dataclasses
import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kiro_crew import beacon
from kiro_crew import loopback_http as _loopback_http
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config import loader as _loader
from kiro_crew.config.loader import (
    ConfigReadError,
    _subtract_overlay,
    config_local_path,
    config_path,
    update_config_locked,
)
from kiro_crew.dashboard import urls as _urls
from kiro_crew.hooks import safe_read_file
from kiro_crew.sel import sel

_MISSING = object()


def _config_cmd(args: argparse.Namespace) -> None:
    """Get or set config values."""
    action = getattr(args, "config_action", None)
    if action == "get":

        cfg = KiroCrewConfig.load()
        d = cfg.to_dict()
        key = getattr(args, "key", None)
        sel().log_api_access(
            caller="cli",
            operation="config_get",
            outcome="allowed",
            source="cli",
            resources=key or "*",
        )
        if not key:
            print(json.dumps(d, indent=2))
            return
        val = _dict_get(d, key)
        if val is _MISSING:
            print(f"❌ Unknown key: {key}", file=sys.stderr)
            sys.exit(1)
        if isinstance(val, (dict, list)):
            print(json.dumps(val, indent=2))
        else:
            print(val)
    elif action == "set":

        file_path = getattr(args, "file", None)
        if file_path:
            fp = Path(file_path).expanduser().resolve()

            try:
                data = json.loads(safe_read_file(str(fp)))
            except PermissionError as e:
                print(f"❌ {e}", file=sys.stderr)
                sys.exit(1)
            except (json.JSONDecodeError, OSError) as e:
                print(f"❌ Invalid JSON: {e}", file=sys.stderr)
                sys.exit(1)
            if not isinstance(data, dict):
                # A JSON array or scalar parses fine but is not a config. Refusing
                # here keeps the file untouched; writing it through would leave a
                # config.json that every reader rejects.
                print(
                    f"❌ Not a config object: {fp} holds a JSON "
                    f"{type(data).__name__}, expected an object",
                    file=sys.stderr,
                )
                sys.exit(1)
            # agent.sandbox requires the gateway to tear down live agent sessions
            # before the new tier takes effect.  A direct disk write (what --file
            # does) cannot do that — an already-running off-mode agent survives
            # with vault access while the config now says "auto".
            #
            # Reject a non-dict "agent" section early: _merge_sandbox would
            # otherwise crash with a TypeError when it tries dict(non_dict).
            _raw_agent = data.get("agent")
            if _raw_agent is not None and not isinstance(_raw_agent, dict):
                print(
                    '❌ Invalid config: "agent" section must be an object, got '
                    f"{type(_raw_agent).__name__}",
                    file=sys.stderr,
                )
                sys.exit(1)
            #
            # Detect BEFORE writing: if the imported file would change
            # agent.sandbox from its current on-disk value, route that change
            # through the gateway PATCH path (the same fail-closed teardown +
            # cap-rotation path the single-key set uses), then strip it from the
            # file data so the remaining keys are written directly.
            #
            # An imported file that OMITS agent.sandbox entirely is treated as
            # requesting the effective default ("auto").  If the current on-disk
            # value is "off" and the import silently drops the key, the file
            # write would leave "off" in place — the exact same bug as an
            # explicit "auto" being ignored.  Using "auto" as the effective
            # imported value ensures the off->auto transition is detected and
            # routed through the gateway teardown path.
            _agent_section = data.get("agent")
            if isinstance(_agent_section, dict) and "sandbox" in _agent_section:
                _new_sandbox = _agent_section["sandbox"]
                # An explicit ``"sandbox": null`` is NOT a distinct tier: the
                # config loader resolves a missing/null sandbox to the effective
                # default ("auto"). Treating null as-is here skips the
                # ``is not None`` routing branch below, so a null imported over a
                # sandbox-"off" base would leave existing unconfined sessions
                # alive while new sessions silently resolve to "auto" — the same
                # missed-teardown bug as a dropped key. Normalize it to "auto"
                # so the off->auto transition is detected and routed.
                if _new_sandbox is None:
                    _new_sandbox = "auto"
            else:
                # Absent sandbox key in the imported file → effective default is "auto".
                _new_sandbox = "auto"
            _routed_sandbox = False
            if _new_sandbox is not None:
                # Read the current on-disk value to compare.
                try:
                    _cur_cfg = KiroCrewConfig.load()
                    _cur_sandbox = (
                        _cur_cfg.agent.sandbox if hasattr(_cur_cfg.agent, "sandbox") else None
                    )
                except Exception:
                    _cur_sandbox = None
                if _new_sandbox != _cur_sandbox:
                    # Sandbox tier would change: route through gateway.  Remove the
                    # key from `data` first so the remaining settings are still
                    # written by the direct path below without the sandbox key.
                    # `or {}` guards `{"agent": null}` (key present, value None):
                    # data.get("agent", {}) returns None there, and dict(None)
                    # would raise TypeError.
                    _agent_section = dict(data.get("agent") or {})
                    _agent_section.pop("sandbox", None)  # may be absent when effective default
                    data = dict(data)
                    if _agent_section:
                        data["agent"] = _agent_section
                    else:
                        data.pop("agent", None)
                    # Gate the sandbox change through the gateway (fail-closed teardown).
                    _config_sandbox_via_gateway(_new_sandbox)
                    _routed_sandbox = True
                    sel().log_api_access(
                        caller="cli",
                        operation="config_set_file_sandbox",
                        outcome="allowed",
                        source="cli",
                        resources=f"agent.sandbox={json.dumps(_new_sandbox)}",
                    )

            def _merge_sandbox(old: dict) -> dict:
                # Preserve the gateway-applied agent.sandbox that was written by
                # _config_sandbox_via_gateway() ONLY when this import actually
                # routed a sandbox change (_routed_sandbox). In that case the
                # gateway already wrote the new tier to disk and a whole-file
                # replacement via `data` (which had the key stripped above) would
                # silently drop it, letting the loader default back to "auto" —
                # the exact race the gateway PATCH path closes.
                #
                # When the import did NOT route a change (the effective tier is
                # unchanged), we must NOT force the old BASE value back: with a
                # config.local.json overlay that shadows agent.sandbox (e.g.
                # overlay "auto" over base "off"), re-applying the stale base
                # "off" here would leave it on disk so that later removing the
                # overlay silently disables sandboxing. Leave `data` as imported.
                result = dict(data)
                if _routed_sandbox:
                    old_agent = old.get("agent", {}) if isinstance(old.get("agent"), dict) else {}
                    if "sandbox" in old_agent:
                        result_agent = dict(result.get("agent") or {})
                        result_agent["sandbox"] = old_agent["sandbox"]
                        result["agent"] = result_agent
                return result

            update_config_locked(config_path(), mutate=_merge_sandbox, on_corrupt="reset")
            sel().log_api_access(
                caller="cli",
                operation="config_set_file",
                outcome="allowed",
                source="cli",
                resources=str(fp),
            )
            print(f"✅ Config loaded from {file_path}")
        else:
            key = args.key
            value = args.value
            use_local = getattr(args, "local", False)
            if not key or value is None:
                print("Usage: kirocrew config set <key> <value>", file=sys.stderr)
                print("       kirocrew config set --local <key> <value>", file=sys.stderr)
                print("       kirocrew config set --file <path.json>", file=sys.stderr)
                sys.exit(1)
            parsed = _parse_value(value)
            # Fourth write path to telemetry.beacon_enabled, after the dashboard
            # PATCH and `telemetry enable`. Gated here too, and BEFORE the
            # local/base split so it covers both: `--local` writes the overlay,
            # which takes precedence over the base file, so leaving it ungated
            # would make the generic setter the one way to store `true` on a
            # pinned host — the same false-promise-on-a-privacy-control failure
            # the 403 exists to prevent. Only the enable direction is refused
            # (tightest-wins), matching the other two chokepoints.
            if key == "telemetry.beacon_enabled" and parsed is True:
                # Audited for the same reason as the other enforcement calls, with
                # its own tool name so the trail says which control refused.
                if beacon.is_governance_pinned_off(audit_tool="config_set_cli"):
                    print(
                        "❌ The anonymous beacon is pinned OFF by your "
                        "administrator's security policy (capabilities.telemetry).",
                        file=sys.stderr,
                    )
                    print(
                        "   Not writing config — the setting would have no effect.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            # Same shape for the tailnet origin derivation, and placed here for the
            # same reason: BEFORE the local/base split, so `--local` (whose overlay
            # takes precedence over the base file) cannot become the one way to
            # store `true` on a pinned host. Only the enable direction is refused
            # (tightest-wins), matching the PATCH 403 and the startup gate.
            if (
                key in ("dashboard.tailscale.enabled", "dashboard.tailscale.trust_identity")
                and parsed is True
            ):
                from kiro_crew.dashboard import tailnet

                if tailnet.is_governance_pinned_off(audit_tool="config_set_cli_tailnet"):
                    print(
                        "❌ Tailnet dashboard access is pinned OFF by your "
                        "administrator's security policy "
                        "(capabilities.tailnet_origin).",
                        file=sys.stderr,
                    )
                    print(
                        "   Not writing config — the setting would have no effect.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            # agent.sandbox requires the gateway to tear down live agent sessions
            # before the new tier takes effect.  A direct disk write (the normal
            # path below) cannot do that — an already-running off-mode agent
            # survives with vault access while the config now says "auto".
            #
            # Route the write through PATCH /api/config/kirocrew, which already
            # performs fail-closed teardown + revert under a single lock (see
            # core.py::api_kirocrew_config_patch).  If the gateway is not running
            # we REFUSE — a direct write would be the exact vulnerability this
            # gate exists to close.
            if key == "agent.sandbox" and use_local:
                # A local-overlay sandbox change is ALSO a direct disk write
                # that cannot tear down live agent sessions — an already-running
                # off-mode agent would survive while the effective (overlaid)
                # config reports "auto", making the CLI secrets floor pass while
                # the surviving agent can still read the vault.  Refuse it: the
                # sandbox tier must be changed through the gateway (drop --local)
                # so the fail-closed teardown runs.
                print(
                    "❌ agent.sandbox cannot be set with --local: a local override "
                    "cannot tear down live agent sessions. Run without --local so "
                    "the change goes through the running gateway's teardown.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if key == "agent.sandbox" and not use_local:
                _config_sandbox_via_gateway(parsed)
                sel().log_api_access(
                    caller="cli",
                    operation="config_set",
                    outcome="allowed",
                    source="cli",
                    resources=f"{key}={json.dumps(parsed)}",
                )
                print(f"✅ {key} = {json.dumps(parsed)}")
                return

            if use_local:
                top_key = key.split(".")[0]
                _known_sections = {f.name for f in dataclasses.fields(KiroCrewConfig)}
                if top_key not in _known_sections:
                    print(
                        f"⚠️  Warning: '{top_key}' is not a recognized config section",
                        file=sys.stderr,
                    )
                p = config_local_path()

                # NOTE: unlike the automatic/background config writers (which now
                # fail closed via read_config_for_update), this interactive path
                # deliberately overwrites a corrupt overlay — the user typed an
                # explicit `config set --local` and sees the result on stdout.
                # Pinned by test_config_overlay.py::TestCliConfigSetLocal.
                #
                # on_corrupt="reset" handles the corrupt case inside the same
                # lock hold: the mutate callback receives {} and writes the
                # single key from scratch. No second critical section needed.
                def _mutate_local_overlay(_existing: dict) -> dict:
                    _dict_set_create(_existing, key, parsed)
                    return _existing

                update_config_locked(
                    p, mutate=_mutate_local_overlay, stamp_meta=False, on_corrupt="reset"
                )

                sel().log_api_access(
                    caller="cli",
                    operation="config_set_local",
                    outcome="allowed",
                    source="cli",
                    resources=f"{key}={json.dumps(parsed)}",
                )
                print(f"✅ {key} = {json.dumps(parsed)} (saved to config.local.json)")
            else:
                # Validate the key exists before taking the lock.
                cfg = KiroCrewConfig.load()
                d = cfg.to_dict()
                if not _dict_set(d, key, parsed):
                    print(f"❌ Unknown key: {key}", file=sys.stderr)
                    sys.exit(1)

                def _mutate_base(existing: dict) -> dict:
                    # Apply the set on the freshly-locked raw data.
                    # Key was already validated above; use _dict_set_create so
                    # sections that were never written (still at defaults) get
                    # their intermediate keys created.
                    _dict_set_create(existing, key, parsed)
                    lp = config_local_path()
                    if lp.is_file():
                        try:
                            raw_local = json.loads(lp.read_text(encoding="utf-8"))
                            if isinstance(raw_local, dict):
                                return _subtract_overlay(existing, raw_local)
                        except (json.JSONDecodeError, OSError):
                            pass
                    return existing

                try:
                    update_config_locked(config_path(), mutate=_mutate_base)
                except ConfigReadError as e:
                    print(
                        f"❌ Cannot set key in a corrupt config.json: {e}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                sel().log_api_access(
                    caller="cli",
                    operation="config_set",
                    outcome="allowed",
                    source="cli",
                    resources=f"{key}={json.dumps(parsed)}",
                )
                print(f"✅ {key} = {json.dumps(parsed)}")
    elif action == "edit":

        p = config_path()
        if not p.exists():
            cfg = KiroCrewConfig()
            cfg.save()
            print(f"👻 Created default config: {p}")
        sel().log_api_access(
            caller="cli",
            operation="config_edit",
            outcome="allowed",
            source="cli",
            resources=str(p),
        )
        editor = os.environ.get("EDITOR", "vi")
        os.execvp(editor, [editor, str(p)])
    else:
        print("Usage: kirocrew config {get,set,edit}", file=sys.stderr)
        sys.exit(1)


def _config_sandbox_via_gateway(value: object) -> None:
    """Route an ``agent.sandbox`` config change through the running gateway.

    The gateway PATCH ``/api/config/kirocrew`` handler performs fail-closed
    teardown of live agent sessions before the new sandbox tier takes effect
    (see ``core.py::api_kirocrew_config_patch``).  A direct disk write cannot
    tear down live sessions, so ``config set agent.sandbox`` MUST always go
    through this path.

    If the gateway is not running the function prints a clear error and exits
    with code 1 — a direct write is NOT offered as a fallback because a direct
    write cannot tear down live unconfined agents, which is the exact
    vulnerability this gate exists to prevent.
    """
    _loopback = "127.0.0.1"
    port = _urls.parse_dashboard_url(KiroCrewConfig.load().dashboard.url)[1]
    secret = _loader.read_local_secret(port)
    if not secret:
        print(
            "❌ Cannot change agent.sandbox without a running gateway to tear down "
            "live agent sessions; start the gateway first with: kirocrew gateway",
            file=sys.stderr,
        )
        sys.exit(1)

    # Mint a short-lived local token.
    token_url = f"http://{_loopback}:{port}/api/token/local?ttl=30s"
    tok_req = urllib.request.Request(token_url, headers={"X-Local-Secret": secret})
    try:
        with _loopback_http.loopback_urlopen(
            tok_req, timeout=5
        ) as resp:  # nosemgrep: dynamic-urllib-use-detected -- hardcoded http:// loopback (127.0.0.1) + fixed internal path; only the int port varies; never agent-controlled  # noqa: E501
            tok_body = json.loads(resp.read())
            bearer = tok_body.get("token", "")
    except (urllib.error.URLError, OSError) as exc:
        print(
            f"❌ Cannot change agent.sandbox without a running gateway "
            f"(could not connect: {exc}); start the gateway first with: kirocrew gateway",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception:
        bearer = ""
    if not bearer:
        print(
            "❌ Cannot change agent.sandbox — could not mint a local token; "
            "is the gateway running?",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = json.dumps({"path": "agent.sandbox", "value": value}).encode()
    # Authenticate via the ?token= query param — token_auth_middleware reads the
    # token from the query string or the mc_token_<port> cookie, not from the
    # Authorization header, so a bearer-only request is rejected. Matches every
    # other local-token caller.
    _tok_q = urllib.parse.quote(bearer, safe="")
    req = urllib.request.Request(
        f"http://{_loopback}:{port}/api/config/kirocrew?token={_tok_q}",
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
    )
    try:
        with _loopback_http.loopback_urlopen(
            req, timeout=10
        ) as resp:  # nosemgrep: dynamic-urllib-use-detected -- hardcoded http:// loopback (127.0.0.1) + fixed internal path; only the int port varies; never agent-controlled  # noqa: E501
            body = json.loads(resp.read())
            if body.get("error"):
                print(f"❌ agent.sandbox change failed: {body['error']}", file=sys.stderr)
                sys.exit(1)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
            err = body.get("error", "request failed")
            code = body.get("code", "")
            msg = f"❌ agent.sandbox change failed: {err}"
            if code:
                msg += f" ({code})"
        except Exception:
            msg = f"❌ agent.sandbox change failed: gateway returned HTTP {exc.code}"
        print(msg, file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, OSError) as exc:
        print(
            f"❌ Cannot change agent.sandbox without a running gateway "
            f"(could not connect: {exc}); start the gateway first with: kirocrew gateway",
            file=sys.stderr,
        )
        sys.exit(1)
    except (ValueError, http.client.IncompleteRead) as exc:
        # A gateway restart mid-response can truncate the body so the read or
        # json.loads() raises (JSONDecodeError subclasses ValueError). Exit
        # cleanly rather than escaping as a traceback; the change's outcome is
        # unknown, so treat it as a failure.
        print(
            f"❌ agent.sandbox change failed: gateway returned an unreadable response "
            f"(outcome unknown: {exc}); re-run once the gateway is stable.",
            file=sys.stderr,
        )
        sys.exit(1)


def _dict_get(d: dict, key: str) -> object:
    """Get a value from a nested dict using dot-separated key."""
    parts = key.split(".")
    cur: object = d
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _dict_set(d: dict, key: str, value: object) -> bool:
    """Set a value in a nested dict using dot-separated key. Returns False if parent missing."""
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    if not isinstance(cur, dict):
        return False
    if parts[-1] not in cur:
        return False
    cur[parts[-1]] = value
    return True


def _dict_set_create(d: dict, key: str, value: object) -> None:
    """Set a value in a nested dict, creating intermediate dicts as needed."""
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _parse_value(raw: str) -> object:
    """Parse a CLI value string into the appropriate Python type."""
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return json.loads(raw)
    except ValueError:
        pass
    return raw
