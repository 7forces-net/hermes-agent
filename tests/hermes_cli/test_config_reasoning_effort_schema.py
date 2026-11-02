"""``agent.reasoning_effort`` is declared in the config schema.

The key was read at runtime by ``hermes_constants.resolve_reasoning_config()`` and
declared in ``cli.py``'s own defaults, but it was ABSENT from ``DEFAULT_CONFIG`` in
``hermes_cli/config_defaults.py``. Consequences:

* ``hermes config set agent.reasoning_effort high`` was REFUSED
  ("'agent.reasoning_effort' is not a recognized config key — nothing was written.
  Did you mean: agent.reasoning_echo"). It only worked with ``--force``, which looks
  like a typo to the next operator.
* ``hermes config check`` treated the key as a missing field.

These tests pin the schema declaration and the resolution behaviour it must not
disturb.
"""

from __future__ import annotations

import pytest


class TestSchemaDeclaration:
    def test_the_key_is_declared_in_the_default_config(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert "reasoning_effort" in DEFAULT_CONFIG["agent"]

    def test_the_default_is_empty(self):
        """Empty = "inherit the provider default" (the pre-change behaviour)."""
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["agent"]["reasoning_effort"] == ""

    def test_the_key_is_recognised_by_the_config_validator(self):
        """This is what makes `hermes config set` accept it without --force."""
        from hermes_cli.config import _validate_config_key

        known, suggestion = _validate_config_key("agent.reasoning_effort")
        assert known is True, f"the key must be recognised (suggestion was {suggestion!r})"

    def test_a_sibling_key_is_still_recognised(self):
        from hermes_cli.config import _validate_config_key

        assert _validate_config_key("agent.reasoning_echo")[0] is True

    def test_the_config_check_does_not_report_it_as_missing(self, tmp_path, monkeypatch):
        from hermes_cli import config as cfg

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        missing = [f["key"] for f in cfg.get_missing_config_fields()]
        assert "agent.reasoning_effort" not in missing

    def test_the_key_can_be_written_without_force(self, tmp_path, monkeypatch, capsys):
        """`hermes config set agent.reasoning_effort high` must succeed as-is."""
        import yaml

        from hermes_cli import config as cfg

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        cfg.set_config_value("agent.reasoning_effort", "high", force=False)
        out = capsys.readouterr().out
        assert "✓ Set agent.reasoning_effort" in out

        raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        assert raw["agent"]["reasoning_effort"] == "high"


class TestResolutionUndisturbed:
    def test_resolve_returns_the_profile_value(self, tmp_path, monkeypatch):
        from hermes_cli.config import load_config
        from hermes_constants import resolve_reasoning_config

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        cfg = load_config()
        cfg.setdefault("agent", {})["reasoning_effort"] = "high"
        assert resolve_reasoning_config(cfg, "some/model") == {"enabled": True, "effort": "high"}

    def test_an_empty_value_resolves_to_none(self):
        """The default (empty) must behave exactly as the ABSENT key did."""
        from hermes_constants import resolve_reasoning_config

        assert resolve_reasoning_config({"agent": {"reasoning_effort": ""}}, "m") is None
        assert resolve_reasoning_config({"agent": {}}, "m") is None

    def test_the_delegation_key_is_unaffected(self):
        """``delegation.reasoning_effort`` is a DIFFERENT key (subagents)."""
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["delegation"]["reasoning_effort"] == ""

    def test_the_per_model_overrides_key_is_unaffected(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["agent"]["reasoning_overrides"] == {}

    def test_a_yaml_false_still_means_disabled(self):
        """``reasoning_effort: false`` must not be coerced back to ""."""
        from hermes_constants import resolve_reasoning_config

        resolved = resolve_reasoning_config({"agent": {"reasoning_effort": False}}, "m")
        assert resolved == {"enabled": False}
