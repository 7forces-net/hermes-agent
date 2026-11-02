"""Regression tests for the misleading fallback-leg diagnostic.

The fallback switcher used to log one flat message for every reason a leg
could not be built:

    Fallback to openai-codex failed: provider not configured

``resolve_provider_client()`` returns ``None`` for at least four very
different situations, and they need different operator responses:

  * the provider was NEVER configured (no credential anywhere) -> provision one
    or drop the leg from the chain;
  * a credential exists but its ACCESS TOKEN IS EXPIRED -> refresh the grant.
    This is the case that actually bit the board: a stale profile-local Codex
    OAuth block shadowed a healthy global-root grant, so the leg was dead for
    57 days while the log insisted the provider was "not configured";
  * a credential exists and is current but the POOL marked it exhausted/dead
    -> clear the exhaustion state or add another credential;
  * anything else.

Collapsing these into one string sent the operator to config.yaml when the
fix lived in auth.json. These tests pin the distinction.

No secret value is asserted or logged anywhere: only key names, expiry
timestamps and status labels.
"""

import base64
import json
import time
from unittest.mock import patch

from agent.chat_completion_helpers import _fallback_leg_unavailable_reason


def _jwt(exp):
    """A minimal unsigned JWT-shaped string carrying *exp* in its payload."""
    def b64(raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = b64(json.dumps({"alg": "none"}).encode())
    payload = b64(json.dumps({"exp": exp}).encode())
    return f"{header}.{payload}.sig"


def _state(tokens):
    return {"tokens": tokens}


class TestNeverConfigured:
    def test_no_credential_block_at_all(self):
        with patch(
            "hermes_cli.auth.get_provider_auth_state", return_value=None
        ):
            reason = _fallback_leg_unavailable_reason("openai-codex")
        assert reason.startswith("never_configured")
        assert "no credential" in reason

    def test_credential_block_present_but_no_tokens(self):
        """A block that exists but carries no token material is still
        'never configured' as far as an operator is concerned."""
        with patch(
            "hermes_cli.auth.get_provider_auth_state",
            return_value={"tokens": {"access_token": "", "refresh_token": ""}},
        ):
            reason = _fallback_leg_unavailable_reason("openai-codex")
        assert reason.startswith("never_configured")

    def test_credential_state_unreadable_is_not_never_configured(self):
        """An unreadable store must not be reported as 'never configured' —
        that would send the operator to provision a credential that exists."""
        with patch(
            "hermes_cli.auth.get_provider_auth_state",
            side_effect=RuntimeError("locked"),
        ):
            reason = _fallback_leg_unavailable_reason("openai-codex")
        assert reason.startswith("unavailable")
        assert "never_configured" not in reason


class TestCredentialsExpired:
    def test_expired_access_token_is_reported_as_expired(self):
        expired = _jwt(int(time.time()) - 3600)
        with (
            patch(
                "hermes_cli.auth.get_provider_auth_state",
                return_value=_state({"access_token": expired, "refresh_token": "rt"}),
            ),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            reason = _fallback_leg_unavailable_reason("openai-codex")
        assert reason.startswith("credentials_expired")
        # The message must say the provider IS configured, or the operator
        # reads it as a config mistake and edits config.yaml.
        assert "IS configured" in reason

    def test_expired_token_message_carries_no_secret(self):
        expired = _jwt(int(time.time()) - 3600)
        with (
            patch(
                "hermes_cli.auth.get_provider_auth_state",
                return_value=_state({"access_token": expired, "refresh_token": "SUPER-SECRET-RT"}),
            ),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            reason = _fallback_leg_unavailable_reason("openai-codex")
        assert "SUPER-SECRET-RT" not in reason
        assert expired not in reason

    def test_expired_token_includes_expiry_timestamp(self):
        expired_at = int(time.time()) - 86400
        with (
            patch(
                "hermes_cli.auth.get_provider_auth_state",
                return_value=_state({"access_token": _jwt(expired_at), "refresh_token": "rt"}),
            ),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            reason = _fallback_leg_unavailable_reason("openai-codex")
        assert "expired" in reason
        assert time.strftime("%Y-%m-%d", time.gmtime(expired_at)) in reason


class TestCredentialsExhausted:
    def test_live_token_but_pool_refuses_to_select(self):
        live = _jwt(int(time.time()) + 86400)
        with (
            patch(
                "hermes_cli.auth.get_provider_auth_state",
                return_value=_state({"access_token": live, "refresh_token": "rt"}),
            ),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            reason = _fallback_leg_unavailable_reason("openai-codex")
        # load_pool returned None -> no selectable entry -> exhausted/dead.
        assert reason.startswith("credentials_exhausted")

    def test_live_token_and_selectable_entry_is_not_exhausted(self):
        live = _jwt(int(time.time()) + 86400)

        class _Pool:
            def select(self):
                return object()

        with (
            patch(
                "hermes_cli.auth.get_provider_auth_state",
                return_value=_state({"access_token": live, "refresh_token": "rt"}),
            ),
            patch("agent.credential_pool.load_pool", return_value=_Pool()),
        ):
            reason = _fallback_leg_unavailable_reason("openai-codex")
        assert reason.startswith("unavailable")
        assert "credentials_exhausted" not in reason


class TestNonJwtToken:
    def test_opaque_token_is_not_treated_as_expired(self):
        """A non-JWT access token must not be guessed expired — that would
        produce a false 'credentials_expired' for a perfectly good credential."""
        with (
            patch(
                "hermes_cli.auth.get_provider_auth_state",
                return_value=_state({"access_token": "opaque-token", "refresh_token": "rt"}),
            ),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            reason = _fallback_leg_unavailable_reason("openai-codex")
        assert reason.startswith("credentials_exhausted")


class TestNoSecretLeak:
    def test_secret_never_appears_in_any_branch(self):
        secret_at = _jwt(int(time.time()) + 86400)
        secret_rt = "REFRESH-TOKEN-SHOULD-NOT-APPEAR"
        for tokens in (
            {"access_token": secret_at, "refresh_token": secret_rt},
            {"access_token": "opaque", "refresh_token": secret_rt},
            {"access_token": "", "refresh_token": secret_rt},
        ):
            with (
                patch(
                    "hermes_cli.auth.get_provider_auth_state",
                    return_value=_state(tokens),
                ),
                patch("agent.credential_pool.load_pool", return_value=None),
            ):
                reason = _fallback_leg_unavailable_reason("openai-codex")
            assert secret_rt not in reason
            assert secret_at not in reason
