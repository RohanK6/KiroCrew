"""Tests for the MCP env-block credential predicates.

These decide whether an agent template may carry a literal secret in an
``mcpServers[].env`` value. They are the security-relevant half of template
authoring and shipped with no coverage, so both directions are pinned here: a
false negative leaks a credential into a world-readable spec, and a false
positive blocks a legitimate config field and pushes users to work around the
check entirely.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import (
    ENV_VAR_REFERENCE_RE,
    MCP_ENV_SECRET_VALUE_RE,
    env_key_is_credential_like,
)


class TestCredentialLikeKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "GITHUB_TOKEN",
            "SLACK_SECRET",
            "DB_PASSWORD",
            "PASSWD",
            "APIKEY",
            "MY_CREDENTIAL",
            "AWS_CREDENTIALS",
            "AUTHORIZATION",
            "API_KEY",
            "ACCESS_KEY",
            "PRIVATE_KEY",
            "AUTH_TOKEN",
            "githubToken",
            "github-token",
        ],
    )
    def test_credential_keys_are_flagged(self, key):
        assert env_key_is_credential_like(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "OAUTH_CLIENT_ID",
            "TOKEN_URL",
            "SECRET_NAME",
            "CREDENTIAL_PATH",
            "AUTH_ENDPOINT",
            "API_KEY_FILE",
            "PASSWORD_HOST",
            "LOG_LEVEL",
            "MCP_SERVER_URI",
        ],
    )
    def test_metadata_keys_are_not_flagged(self, key):
        """A trailing metadata suffix means the value names WHERE a secret lives,
        not the secret. Flagging these blocks legitimate config."""
        assert env_key_is_credential_like(key) is False

    def test_matching_is_token_split_not_substring(self):
        """`TOKENIZER` contains 'TOKEN' as a substring but is not a credential;
        a naive `in` check would flag it."""
        assert env_key_is_credential_like("TOKENIZER") is False
        assert env_key_is_credential_like("AUTHOR") is False

    def test_camel_case_is_split_before_matching(self):
        assert env_key_is_credential_like("myApiKey") is True
        assert env_key_is_credential_like("myApiKeyPath") is False


class TestSecretValuePatterns:
    @pytest.mark.parametrize(
        "value",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "ASIAIOSFODNN7EXAMPLE",
            "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "gho_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "Bearer abcdefghijklmnop",
            "Basic YWxhZGRpbjpvcGVuc2VzYW1l",
            "github_pat_11ABCDEFG0aaaaaaaaaaaaaa",
            "glpat-aaaaaaaaaaaaaaaaaaaaa",
            "xoxb-123456789012-abcdefghij",
            "-----BEGIN RSA PRIVATE KEY",
            "-----BEGIN OPENSSH PRIVATE KEY",
            "postgres://user:hunter2@db.example.com/app",
        ],
    )
    def test_known_secret_shapes_are_caught(self, value):
        assert MCP_ENV_SECRET_VALUE_RE.search(value) is not None

    def test_a_jwt_is_caught(self):
        jwt = "eyJ" + "a" * 22 + ".eyJ" + "b" * 22 + ".sig"
        assert MCP_ENV_SECRET_VALUE_RE.search(jwt) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "https://api.example.com/v1",
            "/usr/local/bin/mcp-server",
            "info",
            "true",
            "3000",
            "en-US",
            "postgres://db.example.com/app",
        ],
    )
    def test_ordinary_config_values_are_not_flagged(self, value):
        assert MCP_ENV_SECRET_VALUE_RE.search(value) is None


class TestEnvVarReference:
    @pytest.mark.parametrize("value", ["${GITHUB_TOKEN}", "$GITHUB_TOKEN", "${A_1}", "$_x"])
    def test_references_are_recognised(self, value):
        assert ENV_VAR_REFERENCE_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "${GITHUB_TOKEN} extra",
            "prefix${VAR}",
            "${}",
            "${1BAD}",
            "$",
            "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ],
    )
    def test_non_references_are_rejected(self, value):
        """The reference form is an ESCAPE from the credential check, so it has to
        be anchored — a value that merely contains `${VAR}` alongside a literal
        secret must not slip through."""
        assert ENV_VAR_REFERENCE_RE.match(value) is None
