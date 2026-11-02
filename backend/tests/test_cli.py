"""CLI: register / projects / status against an isolated CAOBUNGA_HOME."""

import errno
import os
import shlex
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import _commit, _git, _pin_supports_unattended
from test_store import green_suite

import caobunga as caobunga_pkg
import caobunga.cli as cli_module
import caobunga.invocation as invocation_module
import caobunga.status_render as status_render
from caobunga.cli import cli
from caobunga.errors import ReceiptUnwritten, ReportError
from caobunga.flaitron import (
    PARK_UNCOMMITTED,
    Coverage,
    Denial,
    TriageClass,
    parse_plan,
    seeded_line,
)
from caobunga.invocation import InvocationTrace, running_verdicts, stale_receipts
from caobunga.local import LocalWorker
from caobunga.models import (
    CLOSURE_OUTCOMES,
    ExecutionTier,
    ExternalReview,
    HermesVerdictCode,
    InvocationResult,
    InvocationStatus,
    NewRow,
    Outcome,
    RegistryRecord,
    RunRecord,
    utc_now,
)
from caobunga.pins import PinReport
from caobunga.status_render import dump_run_record, format_pins
from caobunga.store import Store
from caobunga.suite_verbs import FitnessResult
from caobunga.worker import ENV_WORKER

_FIT_RESULT = FitnessResult(fit=True, present=("test", "lint", "typecheck"))


def _runner() -> CliRunner:
    return CliRunner()


def test_console_width_holds_on_a_dumb_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rich drops an explicit width on a dumb tty unless height is set too."""
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(cli_module.console, "_force_terminal", True)
    assert cli_module.console.width == 200


def test_version_reports_source_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--version` names the version plus where the package resolves from (CBN-169)."""
    editable = ("editable", Path("/repo/backend/caobunga"))
    monkeypatch.setattr(cli_module, "install_provenance", lambda: editable)
    result = _runner().invoke(cli, ["--version"])
    assert result.exit_code == 0, result.output
    assert "caobunga, version" in result.output
    assert "source: editable /repo/backend/caobunga" in result.output


def test_version_omits_reinstall_hint_on_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--version` reports the path alone; the reinstall hint is `status`'s job."""
    snapshot = ("snapshot", Path("/venv/caobunga"))
    monkeypatch.setattr(cli_module, "install_provenance", lambda: snapshot)
    result = _runner().invoke(cli, ["--version"])
    assert result.exit_code == 0, result.output
    assert "source: snapshot /venv/caobunga" in result.output
    assert "reinstall" not in result.output


def test_status_source_line_editable(monkeypatch: pytest.MonkeyPatch, caobunga_home: Path) -> None:  # noqa: ARG001
    """Fleet `status` prints an editable `source:` line above the table, no reinstall hint."""
    editable = ("editable", Path("/repo/backend/caobunga"))
    monkeypatch.setattr(cli_module, "install_provenance", lambda: editable)
    result = _runner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "source: editable /repo/backend/caobunga hook: present" in result.output
    assert "reinstall" not in result.output


def test_status_source_line_snapshot_carries_reinstall_hint(
    monkeypatch: pytest.MonkeyPatch,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """Fleet `status` under a stale snapshot install names the fix, not just the symptom."""
    snapshot = ("snapshot", Path("/venv/caobunga"))
    monkeypatch.setattr(cli_module, "install_provenance", lambda: snapshot)
    result = _runner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "source: snapshot /venv/caobunga hook: present" in result.output
    assert "reinstall: uv tool install --editable backend --force" in result.output


def test_status_source_line_names_a_missing_hook(
    monkeypatch: pytest.MonkeyPatch,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """Fleet `status` affixes `hook: missing` when this install has no `hooks.py`."""
    editable = ("editable", Path("/repo/backend/caobunga"))
    monkeypatch.setattr(cli_module, "install_provenance", lambda: editable)
    monkeypatch.setattr(cli_module, "hooks_available", lambda: False)
    result = _runner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "source: editable /repo/backend/caobunga hook: missing" in result.output


def test_install_provenance_editable_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real checkout — `.git` ancestor whose `backend/caobunga` matches the package
    dir — is `editable` (CBN-173)."""
    pkg_dir = tmp_path / "backend" / "caobunga"
    pkg_dir.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(caobunga_pkg, "__file__", str(pkg_dir / "__init__.py"))
    assert caobunga_pkg.install_provenance() == ("editable", pkg_dir)


def test_install_provenance_git_rooted_home_is_not_editable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.git`-rooted `$HOME` (dotfiles-as-repo) above an unrelated snapshot install
    must not read as `editable` (CBN-173)."""
    home = tmp_path / "home"
    pkg_dir = home / ".venv" / "lib" / "site-packages" / "caobunga"
    pkg_dir.mkdir(parents=True)
    (home / ".git").mkdir()
    monkeypatch.setattr(caobunga_pkg, "__file__", str(pkg_dir / "__init__.py"))
    assert caobunga_pkg.install_provenance() == ("snapshot", pkg_dir)


def test_register_uses_basename(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "webapp"
    repo.mkdir()
    result = _runner().invoke(cli, ["register", str(repo)])
    assert result.exit_code == 0, result.output
    assert "registered webapp" in result.output
    stored = Store(caobunga_home).get("webapp")
    assert stored.path == str(repo.resolve())
    assert stored.tier.value == "gated"
    assert stored.enabled is True
    assert stored.provider is None
    assert (caobunga_home / "projects" / "webapp.yaml").is_file()
    assert (caobunga_home / "lock").is_file()


def test_register_explicit_id_and_tier(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "webapp"
    repo.mkdir()
    result = _runner().invoke(cli, ["register", str(repo), "--id", "charts", "--tier", "observe"])
    assert result.exit_code == 0, result.output
    stored = Store(caobunga_home).get("charts")
    assert stored.tier.value == "observe"


def test_register_reports_suite_fitness_without_refusing(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """Report-only (CBN-215): a repo with no justfile still registers cleanly."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    result = _runner().invoke(cli, ["register", str(repo)])
    assert result.exit_code == 0, result.output
    assert f"suite-fitness: refused — no justfile at {repo.resolve()}" in result.output


def test_register_warns_when_park_does_not_commit(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """Warn, never refuse (CBN-459): a pin with no committing park still registers."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    result = _runner().invoke(cli, ["register", str(repo)])
    assert result.exit_code == 0, result.output
    assert PARK_UNCOMMITTED.format(id="webapp") in result.output


def test_register_quiet_when_park_commits(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """Through the real probe: the conftest pin carries the `Lone-park commit` step."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    _pin_supports_unattended(repo)
    result = _runner().invoke(cli, ["register", str(repo)])
    assert result.exit_code == 0, result.output
    assert "does not commit" not in result.output


def test_register_reports_suite_fitness_when_fit(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "webapp"
    repo.mkdir()
    (repo / "justfile").write_text("test:\n    echo hi\n", encoding="utf-8")
    monkeypatch.setattr("caobunga.cli.probe_fitness", lambda _repo: _FIT_RESULT)
    result = _runner().invoke(cli, ["register", str(repo)])
    assert result.exit_code == 0, result.output
    assert "suite-fitness: fit — test/lint/typecheck recipes present" in result.output


def test_register_illegal_basename_requires_id(tmp_path: Path, caobunga_home: Path) -> None:  # noqa: ARG001
    repo = tmp_path / "123nope"
    repo.mkdir()
    result = _runner().invoke(cli, ["register", str(repo)])
    assert result.exit_code == 2
    assert "pass --id" in result.output


def test_register_missing_path_is_usage_error(tmp_path: Path, caobunga_home: Path) -> None:  # noqa: ARG001
    result = _runner().invoke(cli, ["register", str(tmp_path / "missing")])
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_register_file_not_directory(tmp_path: Path, caobunga_home: Path) -> None:  # noqa: ARG001
    target = tmp_path / "a-file"
    target.write_text("nope", encoding="utf-8")
    result = _runner().invoke(cli, ["register", str(target)])
    assert result.exit_code == 2
    assert "not a directory" in result.output


def test_replace_required_for_new_path(tmp_path: Path, caobunga_home: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(first), "--id", "app"]).exit_code == 0
    blocked = runner.invoke(cli, ["register", str(second), "--id", "app"])
    assert blocked.exit_code == 2
    assert "--replace" in blocked.output
    replaced = runner.invoke(cli, ["register", str(second), "--id", "app", "--replace"])
    assert replaced.exit_code == 0, replaced.output
    assert Store(caobunga_home).get("app").path == str(second.resolve())


def test_same_id_same_path_updates_tier(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    first = Store(caobunga_home).get("app")
    result = runner.invoke(cli, ["register", str(repo), "--tier", "observe"])
    assert result.exit_code == 0, result.output
    second = Store(caobunga_home).get("app")
    assert second.tier.value == "observe"
    assert second.registered_at == first.registered_at


def test_register_provider_model_pin(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    runner = _runner()
    result = runner.invoke(
        cli,
        ["register", str(repo), "--provider", "grok_cli", "--model", "grok-4.6"],
    )
    assert result.exit_code == 0, result.output
    stored = Store(caobunga_home).get("app")
    assert stored.provider == "grok_cli"
    assert stored.model == "grok-4.6"
    again = runner.invoke(cli, ["register", str(repo), "--tier", "observe"])
    assert again.exit_code == 0, again.output
    kept = Store(caobunga_home).get("app")
    assert kept.tier.value == "observe"
    assert kept.provider == "grok_cli"
    assert kept.model == "grok-4.6"
    # Pins move as a unit (CBN-EPIC-95.4): re-pinning the provider without a
    # --model drops the stale one rather than carrying `grok-4.6` onto
    # claude_code, which is the same class of bug as F0's inert allow-list.
    pinned = runner.invoke(cli, ["register", str(repo), "--provider", "claude_code"])
    assert pinned.exit_code == 0, pinned.output
    updated = Store(caobunga_home).get("app")
    assert updated.provider == "claude_code"
    assert updated.model is None


def test_register_model_requires_provider(tmp_path: Path, caobunga_home: Path) -> None:  # noqa: ARG001
    repo = tmp_path / "app"
    repo.mkdir()
    result = _runner().invoke(cli, ["register", str(repo), "--model", "grok-4.6"])
    assert result.exit_code == 2
    assert "requires --provider" in result.output


def test_register_providers_allow_list(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    runner = _runner()
    result = runner.invoke(cli, ["register", str(repo), "--providers", "claude_code,codex"])
    assert result.exit_code == 0, result.output
    stored = Store(caobunga_home).get("app")
    assert stored.providers == ["claude_code", "codex"]
    assert stored.provider is None
    # A --tier-only re-register must not wipe the list.
    again = runner.invoke(cli, ["register", str(repo), "--tier", "observe"])
    assert again.exit_code == 0, again.output
    kept = Store(caobunga_home).get("app")
    assert kept.tier.value == "observe"
    assert kept.providers == ["claude_code", "codex"]
    # Passing the flag again replaces the list.
    redeclared = runner.invoke(cli, ["register", str(repo), "--providers", "cursor_cli"])
    assert redeclared.exit_code == 0, redeclared.output
    assert Store(caobunga_home).get("app").providers == ["cursor_cli"]


def test_register_providers_normalizes_order_and_duplicates(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    result = _runner().invoke(
        cli,
        ["register", str(repo), "--providers", " codex , claude_code ,codex, "],
    )
    assert result.exit_code == 0, result.output
    assert Store(caobunga_home).get("app").providers == ["codex", "claude_code"]


def test_register_providers_unknown_token_is_usage_error(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    result = _runner().invoke(cli, ["register", str(repo), "--providers", "claude_code,nope"])
    assert result.exit_code == 2
    assert "unknown provider 'nope'" in result.output


def test_register_provider_and_providers_are_mutually_exclusive(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    result = _runner().invoke(
        cli,
        ["register", str(repo), "--provider", "grok_cli", "--providers", "codex"],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_register_providers_blank_value_is_undeclared(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    result = _runner().invoke(cli, ["register", str(repo), "--providers", " , "])
    assert result.exit_code == 0, result.output
    assert Store(caobunga_home).get("app").providers is None


def test_reregister_without_tier_keeps_stored_tier(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-414: only --tier changes the tier; a fresh record still defaults to gated."""
    repo = tmp_path / "app"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    assert Store(caobunga_home).get("app").tier.value == "gated"
    assert runner.invoke(cli, ["register", str(repo), "--tier", "proceed-on-green"]).exit_code == 0
    again = runner.invoke(cli, ["register", str(repo), "--providers", "codex"])
    assert again.exit_code == 0, again.output
    assert "(proceed-on-green)" in again.output
    assert Store(caobunga_home).get("app").tier.value == "proceed-on-green"


def test_register_clear_providers(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo), "--providers", "codex"]).exit_code == 0
    cleared = runner.invoke(cli, ["register", str(repo), "--clear-providers"])
    assert cleared.exit_code == 0, cleared.output
    assert Store(caobunga_home).get("app").providers is None
    for value in ("codex", " , "):
        args = ["register", str(repo), "--providers", value, "--clear-providers"]
        both = runner.invoke(cli, args)
        assert both.exit_code == 2
        assert "mutually exclusive" in both.output


def test_register_bootstrap_flags(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-EPIC-450.3: shlex-split argv, timeout requires it, clear drops both."""
    repo = tmp_path / "app"
    repo.mkdir()
    runner = _runner()
    flags = ["--bootstrap", "just 'set up' $HOME", "--bootstrap-timeout", "90"]
    assert runner.invoke(cli, ["register", str(repo), *flags]).exit_code == 0
    record = Store(caobunga_home).get("app")
    assert record.bootstrap == ["just", "set up", "$HOME"]
    assert record.bootstrap_timeout == 90.0
    kept = runner.invoke(cli, ["register", str(repo), "--tier", "proceed-on-green"])
    assert kept.exit_code == 0, kept.output
    assert Store(caobunga_home).get("app").bootstrap == ["just", "set up", "$HOME"]
    cleared = runner.invoke(cli, ["register", str(repo), "--clear-bootstrap"])
    assert cleared.exit_code == 0, cleared.output
    assert Store(caobunga_home).get("app").bootstrap is None
    assert Store(caobunga_home).get("app").bootstrap_timeout is None


def test_register_isolate_flags(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-EPIC-450.4: `--isolate` / `--no-isolate`; omitting both keeps the stored value."""
    repo = tmp_path / "app"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    assert Store(caobunga_home).get("app").isolate is None
    assert runner.invoke(cli, ["register", str(repo), "--isolate"]).exit_code == 0
    assert Store(caobunga_home).get("app").isolate is True
    kept = runner.invoke(cli, ["register", str(repo), "--tier", "proceed-on-green"])
    assert kept.exit_code == 0, kept.output
    assert Store(caobunga_home).get("app").isolate is True
    assert runner.invoke(cli, ["register", str(repo), "--no-isolate"]).exit_code == 0
    assert Store(caobunga_home).get("app").isolate is False


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--bootstrap-timeout", "5"], "--bootstrap-timeout requires --bootstrap"),
        (["--bootstrap", "x", "--clear-bootstrap"], "--clear-bootstrap conflicts"),
        (["--bootstrap-timeout", "5", "--clear-bootstrap"], "--clear-bootstrap conflicts"),
        (["--bootstrap", "  "], "empty --bootstrap"),
        (["--bootstrap", "just 'unclosed"], "cannot parse --bootstrap"),
        (["--bootstrap", "x", "--bootstrap-timeout", "0"], "Invalid value"),
        (["--bootstrap", "x", "--bootstrap-timeout", "nan"], "must be a finite number"),
        (["--bootstrap", "x", "--bootstrap-timeout", "inf"], "must be a finite number"),
    ],
)
def test_register_bootstrap_usage_errors(
    tmp_path: Path,
    caobunga_home: Path,
    extra: list[str],
    message: str,
) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    result = _runner().invoke(cli, ["register", str(repo), *extra])
    assert result.exit_code == 2
    assert message in result.output
    assert not Store(caobunga_home).project_file("app").exists()


def test_providers_verb_show_set_clear(caobunga_home: Path) -> None:
    runner = _runner()
    shown = runner.invoke(cli, ["providers"])
    assert shown.exit_code == 0, shown.output
    assert shown.output.strip() == "providers: none"
    set_ = runner.invoke(cli, ["providers", "codex, claude_code,codex"])
    assert set_.exit_code == 0, set_.output
    assert set_.output.strip() == "providers: codex, claude_code"
    assert Store(caobunga_home).load_config().providers == ["codex", "claude_code"]
    assert runner.invoke(cli, ["providers"]).output.strip() == "providers: codex, claude_code"
    cleared = runner.invoke(cli, ["providers", "--clear"])
    assert cleared.exit_code == 0, cleared.output
    assert Store(caobunga_home).load_config().providers is None


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["providers", "nope"], "unknown provider 'nope'"),
        (["providers", " , "], "empty provider list"),
        (["providers", "codex", "--clear"], "not both"),
    ],
)
def test_providers_verb_usage_errors(caobunga_home: Path, args: list[str], message: str) -> None:
    result = _runner().invoke(cli, args)
    assert result.exit_code == 2
    assert message in result.output
    assert Store(caobunga_home).load_config().providers is None


def test_same_path_new_id_allowed(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    result = runner.invoke(cli, ["register", str(repo), "--id", "alias"])
    assert result.exit_code == 0, result.output
    ids = {row.id for row in Store(caobunga_home).list_projects()}
    assert ids == {"app", "alias"}


def test_projects_and_status_empty(caobunga_home: Path) -> None:  # noqa: ARG001
    listed = _runner().invoke(cli, ["projects"])
    assert listed.exit_code == 0
    fleet = _runner().invoke(cli, ["status"])
    assert fleet.exit_code == 0


def test_projects_status_and_last_run(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    store = Store(caobunga_home)
    earlier = utc_now().replace(
        year=2026, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    later = utc_now().replace(year=2026, month=2, day=1, hour=0, minute=0, second=0, microsecond=0)
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="a" * 40,
            closed_at=earlier,
            summary="first",
        )
    )
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-010",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="b" * 40,
            closed_at=later,
            summary="second",
        )
    )
    listed = runner.invoke(cli, ["projects"])
    assert listed.exit_code == 0
    assert "webapp" in listed.output
    assert "gated" in listed.output
    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "CBN-010" in fleet.output
    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "tier: gated" in one.output
    assert "CBN-010" in one.output
    assert "second" in one.output
    last = store.last_run("webapp")
    assert last is not None
    assert last.task_id == "CBN-010"


def test_fleet_status_has_no_hermes_verdict_column(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-258 retired the column; a legacy record's code no longer surfaces there."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    store = Store(caobunga_home)
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="a" * 40,
            closed_at=utc_now(),
            summary="first",
            hermes_verdict="VERDICT: accept — sound close.",
            hermes_verdict_code=HermesVerdictCode.ACCEPT,
        )
    )
    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "verdict" not in fleet.output
    assert "accept" not in fleet.output


def test_fleet_status_appends_the_park_reason_to_a_blocked_outcome(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-187: a blocked row names why it stopped, not just that it stopped."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    store = Store(caobunga_home)
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.BLOCKED,
            closed_at=utc_now(),
            summary="parked",
            park_reason="visual-confirm",
        )
    )
    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "blocked (visual-confirm)" in fleet.output


def test_fleet_status_outcome_column_carries_no_affix_with_no_park_reason(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    store = Store(caobunga_home)
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="a" * 40,
            closed_at=utc_now(),
            summary="first",
        )
    )
    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "closed" in fleet.output
    assert "park" not in fleet.output


def test_fleet_status_carries_a_suite_column(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-EPIC-202.5: the last run's deterministic suite verdict, or the dash idiom."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    store = Store(caobunga_home)
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="a" * 40,
            closed_at=utc_now(),
            summary="first",
        )
    )
    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "suite" in fleet.output
    assert "green" in fleet.output


def test_fleet_status_carries_a_review_column(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-257: how the last run's External review box closed, beside `suite`."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    store = Store(caobunga_home)
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            external_review=ExternalReview.MISSING,
            commit_sha="a" * 40,
            closed_at=utc_now(),
            summary="first",
        )
    )
    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "review" in fleet.output
    assert "missing" in fleet.output


def test_fleet_status_suite_column_is_a_dash_with_no_suite(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A park or a failed escalation measures no suite (CBN-EPIC-202.1 D4) — dash, not blank."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    store = Store(caobunga_home)
    store.write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.BLOCKED,
            closed_at=utc_now(),
            summary="parked",
            park_reason="dependency",
        )
    )
    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "green" not in fleet.output
    assert "red" not in fleet.output
    assert "timeout" not in fleet.output
    assert "refused" not in fleet.output


COVERAGE_PLAN = """# sample

## High

- [ ] **CBN-001** [medium] [unattended] | marked — carries the marker.
- [ ] **CBN-002** [medium] | unmarked — no marker.

## Medium

(none)
"""


def _flaitron_repo(tmp_path: Path, plan: str) -> Path:
    repo = tmp_path / "webapp"
    (repo / ".flaitron" / "tasknote").mkdir(parents=True)
    (repo / ".flaitron" / "PLAN.md").write_text(plan, encoding="utf-8")
    return repo


def test_status_reports_marker_coverage_on_both_forms(tmp_path: Path, caobunga_home: Path) -> None:  # noqa: ARG001
    """Fleet form tallies; detail form names the ids an operator would seed."""
    repo = _flaitron_repo(tmp_path, COVERAGE_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "coverage" in fleet.output
    assert "1/2" in fleet.output

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "coverage: 1 marked / 1 denied of 2 open" in one.output
    assert "CBN-002" in one.output
    assert "deny: unmarked" in one.output
    # The marked task is counted, never listed — the block is a seeding worklist.
    assert "CBN-001" not in one.output


def test_status_flags_unparsed_rows_on_both_forms(tmp_path: Path, caobunga_home: Path) -> None:  # noqa: ARG001
    """A grammar-dropped row (CBN-115) is flagged in the fleet cell and named in detail."""
    dropped = "- [ ] **CBN-010** [xheavy] [!unattended] | dropped — bang-shape."
    plan = COVERAGE_PLAN + dropped + "\n"
    repo = _flaitron_repo(tmp_path, plan)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    # Rich may wrap the cell, so assert the word rather than the exact affix.
    assert "unparsed" in fleet.output

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "· 1 unparsed" in one.output
    assert "unparsed: - [ ] **CBN-010**" in one.output


SEEDING_PLAN = """# sample

## High

- [ ] **CBN-020** [medium]🧩 | glyphed — a marker typed at the end would land after the glyph.
- [ ] **CBN-022** | modelless — no [model], so an insertion is captured as the model.
"""


def test_status_shows_where_each_denied_row_is_and_what_it_should_say(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """The detail block is a seeding worklist, not a list of ids (CBN-119.3).

    The glyphed row is the measured majority shape, so the suggestion must show
    `[unattended]` landing *before* the glyph — the placement the natural motion
    gets wrong.
    """
    repo = _flaitron_repo(tmp_path, SEEDING_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "line 5  CBN-020" in one.output
    assert "now:  - [ ] **CBN-020** [medium]🧩" in one.output
    assert "seed: - [ ] **CBN-020** [medium]🧩 [unattended]" in one.output


def test_status_offers_no_seed_line_where_insertion_would_break_the_row(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """CBN-022 has no `[model]`, so a suggestion would be captured as one."""
    repo = _flaitron_repo(tmp_path, SEEDING_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    lines = runner.invoke(cli, ["status", "webapp"]).output.splitlines()
    start = next(i for i, line in enumerate(lines) if "CBN-022" in line and "deny:" in line)
    block = lines[start : start + 3]
    assert any("now:" in line for line in block)
    assert not any("seed:" in line for line in block)


def test_status_elides_a_long_row_so_the_placement_stays_visible(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """A real description runs hundreds of characters; the edit is at the head.

    Printed whole, `now:` and `seed:` wrap into a wall differing by thirteen
    characters near the start. The `Denial` still carries the full line.
    """
    plan = SEEDING_PLAN.replace("a marker typed at the end would land after the glyph.", "x" * 400)
    repo = _flaitron_repo(tmp_path, plan)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    seed = next(line for line in one.output.splitlines() if "seed:" in line)
    assert "[medium]🧩 [unattended]" in seed
    assert seed.rstrip().endswith("…")
    assert "x" * 400 not in one.output


def test_seed_edit_declines_when_the_strings_share_nothing() -> None:
    """No common prefix means no anchor to build a `sed` match from."""
    assert status_render._seed_edit("", "x") is None
    assert status_render._seed_edit("a", "b") is None


def test_seed_edit_declines_when_after_is_not_longer() -> None:
    """`seeded_line` only ever inserts; a same-or-shorter `after` breaks that invariant."""
    assert status_render._seed_edit("**CBN-1** [light]", "**CBN-1** [light]") is None
    assert status_render._seed_edit("**CBN-1** [light] x", "**CBN-1** [light]") is None


def test_seed_command_lines_is_empty_when_no_edit_can_be_built() -> None:
    """A hand-built `Denial` with no real `_seed_edit` still renders no sed line.

    Real `Denial.seeded_line`s always insert (`flaitron.seeded_line`'s own
    contract), so this exercises the constructor's defensive branch directly
    rather than fabricating a `PlanTask` that could never reach it.
    """
    denial = Denial(task_id="CBN-1", reason="unmarked — x", text="", seeded_line="x")
    assert status_render._seed_command_lines(denial) == []


def _extract_seed_script(output: str) -> str:
    """Reassemble `format_seed_block`'s paste-ready lines into a runnable script.

    Rejoins each `grep ... || \\` / `sed ...` pair the block prints as two lines
    (readable to paste) back into one shell statement, dropping the heading and
    the two trailing `#` comments — the same edit an operator would paste, minus
    the human-readable line breaks.
    """
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("seed —"))
    end = next(i for i in range(start, len(lines)) if "commit scoped" in lines[i]) + 1
    script_lines: list[str] = []
    buf = ""
    for raw in lines[start + 1 : end - 2]:
        text = raw.strip()
        if text.endswith("\\"):
            buf += text[:-1] + " "
        else:
            script_lines.append(buf + text)
            buf = ""
    return "\n".join(script_lines)


def test_seed_block_pastes_safely_onto_an_indented_epic_child(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """CBN-157: the paste-ready `sed` block is idempotent and indentation-proof.

    The first hand-written `sed` in the fleet-wide seeding pass this task is
    filed from anchored at column 0 and missed every indented epic child; a
    second lesson from the same pass was a bare `[model]`-anchored `sed` that
    matched again on re-run and doubled the token. Both are exercised here:
    an indented `.2` child, and the same script pasted twice.
    """
    plan = "## High\n\n  - [ ] **CBN-401.2** [medium] | child — an indented epic child.\n"
    repo = _flaitron_repo(tmp_path, plan)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    out = runner.invoke(cli, ["status", "webapp"])
    assert out.exit_code == 0
    script = _extract_seed_script(out.output)
    assert script, "expected a paste-ready seed block for CBN-401.2"
    # CBN-355: the commit hint carries `-m`, so pasting it opens no editor.
    assert 'commit -m "chore: seed [unattended] markers" -- .flaitron/PLAN.md' in out.output
    # CBN-359: the hint names the real path (quoted), not a literal `<path>` placeholder.
    assert f"git -C {shlex.quote(str(repo))} commit -m" in out.output
    assert "git -C <path>" not in out.output
    # BSD's `sed -i ''` (space-separated empty suffix) is a portability trap:
    # GNU sed has no detached `-i` argument, so it reads the empty string as
    # the script and the real script as an unopenable filename (caught in CI,
    # never on a macOS dev machine where this test also runs BSD sed).
    assert "-i.bak" in script
    assert "-i ''" not in script

    plan_path = repo / ".flaitron" / "PLAN.md"
    subprocess.run(["bash", "-c", script], cwd=repo, check=True)  # noqa: S603, S607
    once = plan_path.read_text(encoding="utf-8")
    assert "  - [ ] **CBN-401.2** [medium] [unattended] | child" in once

    subprocess.run(["bash", "-c", script], cwd=repo, check=True)  # noqa: S603, S607
    twice = plan_path.read_text(encoding="utf-8")
    assert twice == once, "pasting the block a second time must be a no-op"

    reparsed = parse_plan(once)
    assert len(reparsed) == 1
    assert reparsed[0].task_id == "CBN-401.2"
    assert reparsed[0].unattended


FLEET_SEED_PLAN = """## High

- [ ] **{p}-001** [medium] | plain — a marker suffices.
  - [ ] **{p}-002.N** [light]🔧 | audit — an indented epic audit child.
- [ ] **{p}-003** [heavy] | weighty — needs a person's read first.
"""


def _named_repo(tmp_path: Path, name: str, plan: str) -> Path:
    repo = tmp_path / name
    (repo / ".flaitron" / "tasknote").mkdir(parents=True)
    (repo / ".flaitron" / "PLAN.md").write_text(plan, encoding="utf-8")
    return repo


def test_fleet_seed_omits_a_keyword_miss(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """CBN-361: a clause 3 keyword miss is not obvious, so `--seed` does not list it.

    CBN-348 still lists only obvious rows. AA-001 and AA-002.N clear every other
    mirrored clause and trip no keyword; that miss is needs-judgment. AA-003 is
    heavy. The clean repo is heavy-only. None of them are obvious. Per-repo
    status still offers the seed.
    """
    alpha = _named_repo(tmp_path, "alpha", FLEET_SEED_PLAN.format(p="AA"))
    clean = _named_repo(tmp_path, "clean", "## High\n\n- [ ] **CC-001** [heavy] | weighty — x.\n")
    runner = _runner()
    for repo in (alpha, clean):
        assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    out = runner.invoke(cli, ["status", "--seed"])
    assert out.exit_code == 0
    assert "seed worklist: none — no obvious unmarked row across 2 read repo(s)" in out.output
    assert "AA-001" not in out.output
    assert "AA-002.N" not in out.output
    assert "AA-003" not in out.output
    assert "clean  (" not in out.output
    assert "sed -i.bak" not in out.output
    # It replaces the table rather than appending to it.
    assert "last_outcome" not in out.output

    detail = runner.invoke(cli, ["status", "alpha"])
    assert detail.exit_code == 0
    assert "triage: keyword — a miss is not a semantic pass" in detail.output
    assert "seed:" in detail.output
    assert "[unattended]" in detail.output


def test_fleet_seed_states_none_and_names_unreadable_repos(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """CBN-125's rule: "nothing to seed" and "could not read" never look alike."""
    clean = _named_repo(tmp_path, "clean", "## High\n\n- [ ] **CC-001** [heavy] | weighty — x.\n")
    bare = tmp_path / "bare"
    bare.mkdir()
    runner = _runner()
    for repo in (clean, bare):
        assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    out = runner.invoke(cli, ["status", "--seed"])
    assert out.exit_code == 0
    assert "seed worklist: none — no obvious unmarked row across 1 read repo(s)" in out.output
    assert "unreadable (not checked): bare" in out.output


def test_fleet_seed_is_a_usage_error_on_the_detail_form(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    repo = _flaitron_repo(tmp_path, COVERAGE_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    out = runner.invoke(cli, ["status", "webapp", "--seed"])
    assert out.exit_code == 2
    assert "--seed is the fleet form" in out.output


def test_fleet_seed_flags_an_obvious_row_with_no_buildable_edit() -> None:
    """A repo whose obvious rows yield no `sed` edit says so rather than going quiet."""
    denial = Denial(
        task_id="CBN-1",
        reason="unmarked — x",
        text="x",
        seeded_line=None,
        triage=TriageClass.OBVIOUS,
    )
    lines = status_render.format_fleet_seed([("solo", "/r", Coverage(denials=[denial]))])
    assert "  seed — no paste-ready edit; seed these rows by hand" in lines
    assert not any("sed -i.bak" in line for line in lines)


def _extract_fleet_seed_script(output: str) -> str:
    """Every `grep ... || \\` / `sed ...` pair in the fleet worklist, one statement each."""
    script_lines: list[str] = []
    buf = ""
    for raw in output.splitlines():
        text = raw.strip()
        if text.startswith("grep -qF"):
            buf = text[:-1] + " "
        elif buf:
            script_lines.append(buf + text)
            buf = ""
    return "\n".join(script_lines)


def _obvious_coverage(plan: str) -> Coverage:
    """Denials the renderer still treats as obvious.

    The reader no longer classifies a keyword miss that way (CBN-361). This
    builds the class by hand so the fleet paste block stays tested.
    """
    denials = [
        Denial(
            task_id=task.task_id,
            reason="unmarked — no [unattended] marker on the PLAN line",
            line=task.line,
            text=task.text,
            seeded_line=seeded_line(task),
            triage=TriageClass.OBVIOUS,
        )
        for task in parse_plan(plan)
        if not task.task_id.endswith("-003")
    ]
    return Coverage(denials=denials)


def test_fleet_seed_block_pastes_safely_from_any_shell(tmp_path: Path) -> None:
    """An injected obvious row's fleet block pastes from an unrelated cwd, twice.

    The reader no longer emits these rows (CBN-361). The paste contract is the
    block, not the triage.
    """
    alpha = _named_repo(tmp_path, "alpha", FLEET_SEED_PLAN.format(p="AA"))
    beta = _named_repo(tmp_path, "beta repo", FLEET_SEED_PLAN.format(p="BB"))
    output = "\n".join(
        status_render.format_fleet_seed(
            [
                ("alpha", str(alpha), _obvious_coverage(FLEET_SEED_PLAN.format(p="AA"))),
                ("beta", str(beta), _obvious_coverage(FLEET_SEED_PLAN.format(p="BB"))),
            ]
        )
    )
    script = _extract_fleet_seed_script(output)
    assert script.count("sed -i.bak") == 4

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["bash", "-c", script], cwd=elsewhere, check=True)  # noqa: S603, S607
    once = {
        repo: (repo / ".flaitron" / "PLAN.md").read_text(encoding="utf-8") for repo in (alpha, beta)
    }
    subprocess.run(["bash", "-c", script], cwd=elsewhere, check=True)  # noqa: S603, S607
    for repo, text in once.items():
        assert (repo / ".flaitron" / "PLAN.md").read_text(encoding="utf-8") == text
        marked = {task.task_id: task.unattended for task in parse_plan(text)}
        assert list(marked.values()) == [True, True, False]
        assert not (repo / ".flaitron" / "PLAN.md.bak").exists(), "backup removed in the target"
    assert not list(elsewhere.iterdir()), "no .bak or stray file left in the paste shell's cwd"


# A blocker in the shape the fleet actually writes (CBN-123 F5) — `Blocked on`,
# which SPEC's `Blocked by [[ID]]` grammar does not parse — pushed past the
# 96-character elision by a realistic description, exactly where every clause
# CBN-123 met was sitting.
BLIND_SEED_PLAN = """# sample

## High

- [ ] **CBN-020** [medium]🧩 | glyphed — {filler} Blocked on [[CBN-050]] — do not start \
before the rows exist.
""".format(filler="padding. " * 12)


def test_status_warns_when_a_row_reads_as_blocked_but_parsed_no_clause(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """CBN-128: the worklist said *where* to seed and was silent on *whether*.

    CBN-123 seeded three rows on this report's own recommendation; all three were
    genuinely blocked in prose the grammar drops, and every clause sat past the
    elision. The row still denies as `unmarked` — the warning is a claim about
    prose, not a gate.
    """
    repo = _flaitron_repo(tmp_path, BLIND_SEED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "deny: unmarked" in one.output
    assert 'warn: says "blocked", no `Blocked by [[ID]]` clause parsed' in one.output
    assert "do not start before the rows exist" in one.output


def test_status_warning_spends_its_budget_on_the_clause_not_the_decorations(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """The tail signal appears without costing the head one (CBN-119.3 preserved).

    `now:` and `seed:` still elide at the decoration run, so marker *placement*
    stays visible; the `warn:` fragment starts at the mention, so the same 96
    characters buy the text that decides eligibility.
    """
    repo = _flaitron_repo(tmp_path, BLIND_SEED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    lines = runner.invoke(cli, ["status", "webapp"]).output.splitlines()
    now = next(line for line in lines if "now:" in line)
    seed = next(line for line in lines if "seed:" in line)
    quoted = next(line for line in lines if "Blocked on [[CBN-050]]" in line)

    # The head rows are unchanged: elided, and still showing the placement.
    assert now.rstrip().endswith("…")
    assert "[medium]🧩 [unattended]" in seed
    assert "Blocked on" not in now, "the clause is past the elision — that is the bug"
    # The clause the operator needs is quoted, and starts at the mention.
    assert quoted.strip().startswith("Blocked on [[CBN-050]]")
    assert "padding." not in quoted


def test_status_still_offers_the_seed_line_beside_the_warning(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """Report-only: Caobunga does not decide a producer's sentence means blocked.

    Withholding the fix on a reading of someone else's prose is the §"Fleet
    boundary" line that widening the grammar was rejected for. Both rows print
    and the operator decides.
    """
    repo = _flaitron_repo(tmp_path, BLIND_SEED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    one = runner.invoke(cli, ["status", "webapp"])
    assert "warn:" in one.output
    assert "seed: - [ ] **CBN-020** [medium]🧩 [unattended]" in one.output


def test_status_adds_no_warning_to_a_row_with_a_parsed_clause(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """Its `deny:` reason already says `blocked` — the warning would only repeat it."""
    plan = BLIND_SEED_PLAN.replace("Blocked on [[CBN-050]]", "Blocked by [[CBN-050]]")
    repo = _flaitron_repo(tmp_path, plan)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    one = runner.invoke(cli, ["status", "webapp"])
    assert "deny: blocked — Blocked by [[CBN-050]]" in one.output
    assert "warn:" not in one.output


DRIFTED_PLAN = """# sample

## High

- [ ] **CBN-001** [medium] | disarmed — a rewrite dropped this row's marker.
- [ ] **CBN-002** [medium] [unattended] | intact — still armed.
"""


def _baseline(caobunga_home: Path, marked: list[str]) -> None:
    """A run record carrying the marked set `caobunga status` diffs against."""
    Store(caobunga_home).write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-002",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="c" * 40,
            closed_at=utc_now(),
            summary="baseline",
            marked_tasks=marked,
        )
    )


def test_status_flags_a_row_that_lost_its_marker_since_the_last_run(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-125: an unrelated line edit disarmed a seeded cohort; say so before a drain.

    Observed live twice on the same rows inside one CBN-120.2 trial, when
    one fleet repo fell 3 marked → 0 while the run sat parked.
    """
    repo = _flaitron_repo(tmp_path, DRIFTED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    _baseline(caobunga_home, ["CBN-001", "CBN-002"])

    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "drifted" in fleet.output

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "drift: 1 of 2 row(s) marked at the last run" in one.output
    assert "CBN-002, 20" in one.output  # the baseline run is named, with its date
    assert "line 5  CBN-001  deny: unmarked" in one.output
    assert "seed: - [ ] **CBN-001** [medium] [unattended]" in one.output


def test_status_states_an_intact_cohort_rather_than_going_quiet(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """A guard that prints nothing cannot be told from one that found nothing."""
    plan = DRIFTED_PLAN.replace("**CBN-001** [medium] |", "**CBN-001** [medium] [unattended] |")
    repo = _flaitron_repo(tmp_path, plan)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    _baseline(caobunga_home, ["CBN-001", "CBN-002"])

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "drift: none — all 2 row(s) marked at the last run" in one.output
    # Nothing drifted, so the fleet cell keeps the compact form.
    assert "drifted" not in runner.invoke(cli, ["status"]).output


def test_status_says_when_there_is_no_baseline_to_diff_against(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """No run, or a record predating the field: the guard names its own blind spot."""
    repo = _flaitron_repo(tmp_path, DRIFTED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "drift: (no baseline" in one.output

    _baseline(caobunga_home, [])
    again = runner.invoke(cli, ["status", "webapp"])
    assert again.exit_code == 0
    assert "drift: (no baseline" in again.output


def test_status_coverage_is_unreadable_for_a_non_flaitron_path(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """A registered path with no PLAN costs the figure, not the command."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    assert runner.invoke(cli, ["status"]).exit_code == 0
    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "coverage: (unreadable)" in one.output


TREE_PLAN = """# sample

## High

- [ ] **CBN-001** [medium] [unattended] | head — first in dispatch order.
- [ ] **CBN-002** [medium] [unattended] | second — carries the open note.
"""


def _git_flaitron_repo(tmp_path: Path, plan: str) -> Path:
    """A `_flaitron_repo` that is also a committed git repo, so the tree reads."""
    repo = _flaitron_repo(tmp_path, plan)
    _git(repo, "init")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--no-gpg-sign",
        "-m",
        "init",
    )
    return repo


def test_status_reports_target_tree_state_on_both_forms(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """The gate's verdict, before a launch has to reveal it (CBN-130)."""
    repo = _git_flaitron_repo(tmp_path, TREE_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "tree" in fleet.output
    assert "clean" in fleet.output

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "tree: clean — nothing would refuse a dispatch of CBN-001" in one.output


def test_status_names_the_coupling_between_the_seeded_row_and_foreign_dirt(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """The finding, rendered (CBN-123 F3 → CBN-130).

    `CBN-002`'s open tasknote is exempt under `gated` — the in-progress override
    makes it the candidate — and foreign dirt under `proceed-on-green`, where the
    queue head wins the pick. The tree is byte-identical in both cases; only the
    candidate moved. Before this the operator learned it from an exit 2.
    """
    repo = _git_flaitron_repo(tmp_path, TREE_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-002.md").write_text(
        "---\nstatus: in-progress\n---\n", encoding="utf-8"
    )
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo), "--tier", "proceed-on-green"]).exit_code == 0

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "tree: dirty — 1 change(s) would refuse this dispatch (exit 2)" in one.output
    assert "candidate: CBN-001 (no open tasknote — nothing exempt)" in one.output
    assert "?? .flaitron/tasknote/CBN-002.md" in one.output
    assert "open tasknote for CBN-002; dirt only because the candidate is CBN-001" in one.output
    assert "note: the gate reads clean only for a dispatch of CBN-002" in one.output

    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "dirty (1)" in fleet.output
    # No run owns CBN-002, so its note is not a filing (CBN-434): the F1 shape.
    assert "filing artifact" not in one.output


def test_status_distinguishes_an_unreadable_tree_from_a_clean_one(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    """CBN-125's rule, applied to the third state: never render silence."""
    repo = _flaitron_repo(tmp_path, TREE_PLAN)  # a Flaitron project, not a git repo
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "tree: unreadable — git could not report" in one.output
    assert "tree: clean" not in one.output


def _running_receipt(store: Store, project: str, pid: int | None) -> InvocationResult:
    """A `running` checkpoint, as a live run or an uncatchable death leaves it."""
    trace = InvocationTrace(project, store)
    trace.result.pid = pid
    trace.checkpoint()
    return trace.result


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])  # noqa: S607
    proc.wait()
    return proc.pid


def test_status_receipts_name_each_running_receipt_with_its_verdict(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """CBN-421: a dead run shows on `status`, not only on the slug's next `run`."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    store = Store(caobunga_home)
    live = _running_receipt(store, "webapp", os.getpid())
    dead_pid = _dead_pid()
    stale = _running_receipt(store, "webapp", dead_pid)
    legacy = _running_receipt(store, "webapp", None)
    _running_receipt(store, "unregistered", _dead_pid())

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0, one.output
    assert "receipts: 3 at running (1 live, 1 stale, 1 unknown)" in one.output
    assert f"  {live.receipt_id} live — pid {os.getpid()}, since " in one.output
    assert f"  {stale.receipt_id} stale — pid {dead_pid} gone, since " in one.output
    assert "last stage running; the next `caobunga run webapp` reconciles it" in one.output
    # No pid and no registered path: the lock never proves it dead, so no run will.
    assert f"  {legacy.receipt_id} unknown — pid unrecorded, since " in one.output
    assert "no `run` reconciles it: no pid, and not the registered path" in one.output

    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0, fleet.output
    stale_out = [line for line in fleet.output.splitlines() if line.startswith("stale:")]
    assert len(stale_out) == 1
    assert stale_out[0].startswith(f"stale: webapp {stale.receipt_id} — pid {dead_pid} gone")

    # Report-only: the rewrite stays `run`'s, under its lock.
    assert store.load_invocation(stale.receipt_id).status.value == "running"


def test_status_names_what_landed_behind_a_stale_receipt(
    tmp_path: Path, caobunga_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CBN-444: a dead run's commits show on `status`, both forms, before `run` heals it."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    store = Store(caobunga_home)
    _running_receipt(store, "webapp", os.getpid())
    stale = _running_receipt(store, "webapp", _dead_pid())
    landed = ["ebab88a feat: NAS-12.N closed", "1234567 docs: status card"]
    monkeypatch.setattr(invocation_module, "commits_since", lambda _root, _since: landed)

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0, one.output
    lines = one.output.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith(f"  {stale.receipt_id} stale"))
    assert lines[at + 1] == (
        "    landed since start: 2 commit(s) — ebab88a feat: NAS-12.N closed; "
        "1234567 docs: status card"
    )
    # Only the stale receipt gets one: a live run's commits are its own business.
    assert sum("landed since start" in line for line in lines) == 1

    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0, fleet.output
    [footer] = [line for line in fleet.output.splitlines() if line.startswith("stale:")]
    assert footer.endswith("reconciles it; landed since start: 2 commit(s)")

    assert store.load_invocation(stale.receipt_id).status.value == "running"


def test_stale_receipts_read_landed_commits_from_the_receipts_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real git: a commit after `started_at` is named; live, pid-less and other slugs are not."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    _git(repo, "init")
    store = Store(tmp_path / "home")
    stale = _running_receipt(store, "webapp", _dead_pid())
    stale.path = str(repo)
    (repo / "landed.py").write_text("landed\n", encoding="utf-8")
    later = (stale.started_at + timedelta(minutes=5)).isoformat()
    monkeypatch.setenv("GIT_COMMITTER_DATE", later)
    monkeypatch.setenv("GIT_AUTHOR_DATE", later)
    _commit(repo, "feat: landed")
    # The slug moved since: landed is read from the receipt's own path.
    record = RegistryRecord(id="webapp", path=str(tmp_path / "moved"))
    live = _running_receipt(store, "webapp", os.getpid())
    legacy = _running_receipt(store, "webapp", None)
    other = _running_receipt(store, "other", _dead_pid())

    verdicts = [(stale, False), (live, True), (legacy, None), (other, False)]
    [item] = stale_receipts(record, verdicts)

    assert item.receipt_id == stale.receipt_id
    assert item.pid == stale.pid
    assert item.last_stage == "running"
    assert item.landed is not None
    assert [line.split(" ", 1)[1] for line in item.landed] == ["feat: landed"]


def test_landed_line_keeps_unreadable_apart_from_zero() -> None:
    assert status_render.landed_line(None) == "landed since start: unreadable"
    assert status_render.landed_line([]) == "landed since start: 0 commit(s) — nothing"
    many = [f"{n:07d} fix: {n}" for n in range(7)]
    assert status_render.landed_line(many) == (
        "landed since start: 7 commit(s) — " + "; ".join(many[:5]) + " (+2 more)"
    )


@pytest.mark.parametrize(
    ("update", "hint"),
    [
        ({}, "the next `caobunga run app` reconciles it"),
        ({"tier": ExecutionTier.OBSERVE}, "no `run` reconciles it at tier observe"),
        ({"enabled": False}, "no `run` reconciles it while app is disabled"),
    ],
)
def test_stale_hint_follows_the_gates_run_would_take(
    tmp_path: Path, update: dict[str, object], hint: str
) -> None:
    """An observe or disabled project never reaches `reconcile_stale`; the hint says so."""
    store = Store(tmp_path)
    receipt = _running_receipt(store, "app", _dead_pid())
    record = RegistryRecord(id="app", path=str(tmp_path)).model_copy(update=update)
    [line] = status_render.stale_lines(record, [(receipt, False)])
    assert line.endswith(hint)


def test_status_drops_a_dead_pid_whose_receipt_finished_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that wrote its terminal receipt and exited after the scan is not stale."""
    store = Store(tmp_path)
    receipt = _running_receipt(store, "app", _dead_pid())
    finished = receipt.model_copy(
        update={
            "status": InvocationStatus.COMPLETE,
            "finished_at": utc_now(),
            "legacy_exit_code": 0,
            "successful": True,
        }
    )
    monkeypatch.setattr(store, "load_invocation", lambda _id: finished)
    assert running_verdicts(store, "app") == []
    monkeypatch.setattr(store, "load_invocation", lambda _id: receipt)
    assert running_verdicts(store, "app") == [(receipt, False)]


def test_status_receipts_say_none_running_rather_than_going_quiet(
    tmp_path: Path, caobunga_home: Path
) -> None:
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    _running_receipt(Store(caobunga_home), "other", _dead_pid())
    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0, one.output
    assert "receipts: none running" in one.output
    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0, fleet.output
    assert "stale:" not in fleet.output


def test_status_unknown_project(caobunga_home: Path) -> None:  # noqa: ARG001
    result = _runner().invoke(cli, ["status", "nope"])
    assert result.exit_code == 2
    assert "unknown project" in result.output


def test_format_pins_reports_clean_when_nothing_installed() -> None:
    assert format_pins([PinReport(provider="grok_cli")]) == ["pins: (no installed provider)"]


def test_format_pins_names_a_stale_id() -> None:
    lines = format_pins([PinReport(provider="grok_cli", installed=True, stale=("grok-4.5",))])
    assert lines == [
        "pins: 1 installed provider(s) checked",
        "pins: grok_cli grok-4.5 not in catalog",
    ]


def test_format_pins_names_a_partial_catalog_without_a_stale_claim() -> None:
    lines = format_pins([PinReport(provider="codex", installed=True, partial=True)])
    assert lines == [
        "pins: 1 installed provider(s) checked",
        "pins: codex catalog partial (TAP-gated ids hidden)",
    ]


def test_format_pins_names_an_unavailable_catalog() -> None:
    lines = format_pins(
        [PinReport(provider="claude_code", installed=True, message="catalog unavailable")]
    )
    assert lines == [
        "pins: 1 installed provider(s) checked",
        "pins: claude_code catalog unavailable",
    ]


def test_format_pins_a_clean_installed_provider_adds_no_second_line() -> None:
    lines = format_pins([PinReport(provider="grok_cli", installed=True)])
    assert lines == ["pins: 1 installed provider(s) checked"]


def test_status_detail_prints_pins_lines(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CBN-166: the per-project detail gains a `pins:` block; the fleet table does not."""
    repo = tmp_path / "webapp"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    monkeypatch.setattr(
        cli_module.pins,
        "check_all",
        lambda: [
            PinReport(provider="codex", installed=True, partial=True),
            PinReport(provider="claude_code", installed=True, message="catalog unavailable"),
        ],
    )
    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "pins: 2 installed provider(s) checked" in one.output
    assert "pins: codex catalog partial (TAP-gated ids hidden)" in one.output
    assert "pins: claude_code catalog unavailable" in one.output

    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "pins" not in fleet.output


def _sample_run(
    *,
    task_id: str = "CBN-011",
    outcome: Outcome = Outcome.CLOSED,
    commit_sha: str | None = "a" * 40,
    provider: str | None = "grok_cli",
    model: str | None = "grok-4.6",
    next_task: str | None = "CBN-012",
    summary: str = "The reader picks the next task.",
) -> RunRecord:
    closed_at = utc_now().replace(
        year=2026, month=8, day=19, hour=0, minute=0, second=0, microsecond=0
    )
    return RunRecord(
        project="app",
        task_id=task_id,
        outcome=outcome,
        commit_sha=commit_sha,
        closed_at=closed_at,
        provider=provider,
        model=model,
        plan_model="medium",
        next_task=next_task,
        tasknote_path=".flaitron/tasknote/archive/cbn/CBN-011.md",
        summary=summary,
        suite=green_suite() if outcome in CLOSURE_OUTCOMES else None,
    )


def test_dump_run_record_yaml_and_omits_none() -> None:
    dumped = dump_run_record(_sample_run())
    assert dumped.startswith("schema_version: 2\n")
    assert "task_id: CBN-011" in dumped
    assert "outcome: closed" in dumped
    assert "commit_sha: " + ("a" * 40) in dumped
    assert "next_task: CBN-012" in dumped
    assert "summary: The reader picks the next task." in dumped
    blocked = dump_run_record(
        _sample_run(
            outcome=Outcome.BLOCKED,
            commit_sha=None,
            provider=None,
            model=None,
            next_task=None,
            summary="tasknote is blocked; not launching",
        )
    )
    keys = {line.split(":", 1)[0] for line in blocked.splitlines() if ":" in line}
    assert "outcome" in keys
    assert "plan_model" in keys
    assert "commit_sha" not in keys
    assert "provider" not in keys
    assert "model" not in keys
    assert "next_task" not in keys


def test_run_prints_run_record(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _sample_run()
    monkeypatch.setattr("caobunga.cli.execute_run", lambda *args, **kwargs: record)  # noqa: ARG005
    result = _runner().invoke(cli, ["run", "app"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("run:\n")
    assert "task_id: CBN-011" in result.output
    assert "outcome: closed" in result.output
    assert "sha=" not in result.output


def test_run_report_error_dumps_then_exit_4(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _sample_run()

    def boom(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        raise ReportError("card write failed", record)

    monkeypatch.setattr("caobunga.cli.execute_run", boom)
    result = _runner().invoke(cli, ["run", "app"])
    assert result.exit_code == 4
    assert "outcome: closed" in result.output
    assert "task_id: CBN-011" in result.output
    assert "card write failed" in result.output


def test_run_failed_dumps_then_exit_4(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _sample_run(
        outcome=Outcome.FAILED,
        commit_sha=None,
        next_task=None,
        summary="timed out after 2s waiting for CBN-011 to close",
    )
    monkeypatch.setattr("caobunga.cli.execute_run", lambda *args, **kwargs: record)  # noqa: ARG005
    result = _runner().invoke(cli, ["run", "app"])
    assert result.exit_code == 4
    assert "outcome: failed" in result.output
    assert "timed out after 2s" in result.output
    assert "commit_sha:" not in result.output


@pytest.mark.parametrize("mode", [None, "local", "kubernetes"])
def test_run_selects_worker_inside_durable_invocation(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str | None,
) -> None:
    from conftest import OPEN_PLAN, _git_repo, _green_suite, _register

    from caobunga.worker import select_worker

    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    seen = []
    # Past the pre-launch baseline (CBN-420) to the selection under test.
    monkeypatch.setattr(
        "caobunga.finish.run_fleet_suite", lambda root, st, **_kw: _green_suite(root, st)
    )

    def select(home: Path):
        worker = select_worker(home)
        seen.append(worker)
        return worker

    if mode is None:
        monkeypatch.delenv(ENV_WORKER, raising=False)
    else:
        monkeypatch.setenv(ENV_WORKER, mode)
    monkeypatch.setattr("caobunga.loop.select_worker", select)
    # Stop before any real worker launch while exercising selection and receipt handling.
    monkeypatch.setattr(
        "caobunga.launch.pick",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("no test worker")),
    )
    result = _runner().invoke(cli, ["run", "app"])
    assert result.exit_code == 2
    files = list((caobunga_home / "invocations").glob("*.yaml"))
    assert len(files) == 1
    receipt = store.load_invocation(files[0].stem)
    assert not receipt.successful and not receipt.run_records
    assert "invocation-record:" in result.output and "recovery:" in result.output
    if mode == "kubernetes":
        assert "CAOBUNGA_WORKER" in result.output
        assert receipt.stop_reason == "worker-start"
    else:
        assert isinstance(seen[0], LocalWorker)
        assert receipt.stop_reason == "provider"


def test_digest_empty_registry(caobunga_home: Path) -> None:  # noqa: ARG001
    result = _runner().invoke(cli, ["digest"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Fleet digest — ")
    assert "(no registered projects)" in result.output


def test_digest_appends_model_pins_block(
    caobunga_home: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CBN-256: the CLI verb (not `format_digest`) ends with the `pins:` lines."""
    monkeypatch.setattr(
        cli_module.pins,
        "check_all",
        lambda: [PinReport(provider="grok_cli", installed=True, stale=("grok-4.6",))],
    )
    result = _runner().invoke(cli, ["digest"])
    assert result.exit_code == 0, result.output
    body, _, tail = result.output.partition("\n## Model pins\n\n")
    assert "(no registered projects)" in body
    assert tail == (
        "- pins: 1 installed provider(s) checked\n- pins: grok_cli grok-4.6 not in catalog\n"
    )


def test_digest_prints_today_runs(tmp_path: Path, caobunga_home: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo), "--id", "app"]).exit_code == 0
    Store(caobunga_home).write_run(
        RunRecord(
            project="app",
            task_id="CBN-031",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="a" * 40,
            closed_at=utc_now(),
            summary="Epic rollup writer.",
        )
    )
    result = runner.invoke(cli, ["digest"])
    assert result.exit_code == 0, result.output
    assert "## app" in result.output
    assert "**CBN-031** (`closed`) — Epic rollup writer." in result.output


def test_run_unknown_project(caobunga_home: Path) -> None:  # noqa: ARG001
    result = _runner().invoke(cli, ["run", "nope"])
    assert result.exit_code == 2
    assert "unknown project" in result.output


def test_run_unwritten_receipt_is_one_named_line_and_exit_4(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001 — isolates the store
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CBN-442: a full disk at the receipt write is named, not a traceback."""
    from conftest import EXHAUSTED_PLAN, _git_repo

    repo = _git_repo(tmp_path / "exh", EXHAUSTED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo), "--id", "exh"]).exit_code == 0

    def full(*_args: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Store, "write_invocation", full)
    result = runner.invoke(cli, ["run", "exh"])
    assert result.exit_code == 4, result.output
    assert "invocation-record-unwritten: No space left on device\n" in result.output
    assert "recovery: fix what stopped the write" in result.output
    assert "Traceback" not in result.output
    assert "invocation-record: " not in result.output


def test_run_unwritten_receipt_still_dumps_the_last_run_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CBN-442 review: with no receipt, the record is what says what committed."""
    record = _sample_run()

    def full(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        raise ReceiptUnwritten("No space left on device", record=record)

    monkeypatch.setattr("caobunga.cli.execute_run", full)
    result = _runner().invoke(cli, ["run", "app"])
    assert result.exit_code == 4
    assert "task_id: CBN-011" in result.output
    assert "invocation-record-unwritten: No space left on device" in result.output


def test_run_observe_and_exhausted(tmp_path: Path, caobunga_home: Path) -> None:
    from conftest import EXHAUSTED_PLAN, OPEN_PLAN, _git_repo

    observed = _git_repo(tmp_path / "obs", OPEN_PLAN)
    empty = _git_repo(tmp_path / "exh", EXHAUSTED_PLAN)
    runner = _runner()
    observed_reg = runner.invoke(
        cli, ["register", str(observed), "--id", "obs", "--tier", "observe"]
    )
    assert observed_reg.exit_code == 0
    assert runner.invoke(cli, ["register", str(empty), "--id", "exh"]).exit_code == 0
    status = runner.invoke(cli, ["run", "obs"])
    assert status.exit_code == 0, status.output
    assert "next: CBN-011" in status.output
    assert Store(caobunga_home).last_run("obs") is None
    exhausted = runner.invoke(cli, ["run", "exh"])
    assert exhausted.exit_code == 3
    assert "no next task" in exhausted.output
    green = _git_repo(tmp_path / "green", EXHAUSTED_PLAN)
    green_reg = runner.invoke(
        cli, ["register", str(green), "--id", "green", "--tier", "proceed-on-green"]
    )
    assert green_reg.exit_code == 0
    later = runner.invoke(cli, ["run", "green"])
    assert later.exit_code == 3
    assert "no next task" in later.output


def test_run_dumps_every_record_not_just_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """CBN-EPIC-9.5 F3. A later-tier run that dies on close 2 must still show close 2.

    The CBN-EPIC-9.4 symptom: close 1 printed, close 2 succeeded and was written to
    disk, then a ReportError aborted the run — and the operator saw only close 1, so
    work that succeeded looked like work that never ran.
    """
    first = _sample_run()
    second = _sample_run(task_id="CBN-012", summary="second close")

    def two_closes(*args: object, **kwargs: object) -> RunRecord:  # noqa: ARG001
        on_run = kwargs["on_run"]
        assert callable(on_run)
        on_run(first)
        raise ReportError("status card write failed: boom", second)

    monkeypatch.setattr("caobunga.cli.execute_run", two_closes)
    result = _runner().invoke(cli, ["run", "app"])
    assert result.exit_code == 4
    assert "task_id: CBN-011" in result.output
    assert "task_id: CBN-012" in result.output
    assert result.output.count("run:") == 2


def test_run_does_not_dump_the_same_record_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ReportError from a close's card write carries an already-dumped record."""
    record = _sample_run()

    def flush_fails(*args: object, **kwargs: object) -> RunRecord:  # noqa: ARG001
        on_run = kwargs["on_run"]
        assert callable(on_run)
        on_run(record)
        raise ReportError("card write failed", record)

    monkeypatch.setattr("caobunga.cli.execute_run", flush_fails)
    result = _runner().invoke(cli, ["run", "app"])
    assert result.exit_code == 4
    assert result.output.count("run:") == 1


def test_run_dumps_each_distinct_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedup is by identity, never a task_id-keyed flag: a real second close dumps."""
    first = _sample_run(task_id="CBN-011")
    second = _sample_run(task_id="CBN-011", summary="a later close")

    def two_closes(*args: object, **kwargs: object) -> RunRecord:  # noqa: ARG001
        on_run = kwargs["on_run"]
        assert callable(on_run)
        on_run(first)
        return second

    monkeypatch.setattr("caobunga.cli.execute_run", two_closes)
    result = _runner().invoke(cli, ["run", "app"])
    assert result.exit_code == 0, result.output
    assert result.output.count("run:") == 2


def _new_rows_baseline(caobunga_home: Path, rows: list[NewRow] | None) -> None:
    """A run record carrying the new-row measurement `caobunga status` reports."""
    Store(caobunga_home).write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-002",
            outcome=Outcome.CLOSED,
            suite=green_suite(),
            commit_sha="c" * 40,
            closed_at=utc_now(),
            summary="baseline",
            new_rows=rows,
        )
    )


def test_status_names_the_rows_the_last_run_filed(tmp_path: Path, caobunga_home: Path) -> None:
    """CBN-138.3: a drain's discoveries surface before the picker denies them."""
    repo = _flaitron_repo(tmp_path, DRIFTED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    _new_rows_baseline(
        caobunga_home,
        [NewRow(task_id="CBN-014"), NewRow(task_id="CBN-015", unattended=True)],
    )

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "new rows: 2 filed during the last run" in one.output
    assert "1 unmarked" in one.output
    assert "CBN-014  unmarked — seed [unattended] to dispatch" in one.output
    assert "CBN-015  marked" in one.output


def test_status_states_that_the_last_run_filed_nothing(tmp_path: Path, caobunga_home: Path) -> None:
    """Measured-and-empty is stated, not silent — the CBN-125 always-emit rule."""
    repo = _flaitron_repo(tmp_path, DRIFTED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    _new_rows_baseline(caobunga_home, None)

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "new rows: none filed during the last run" in one.output


def test_status_states_when_no_run_record_carries_a_measurement(
    tmp_path: Path,
    caobunga_home: Path,  # noqa: ARG001
) -> None:
    repo = _flaitron_repo(tmp_path, DRIFTED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0

    one = runner.invoke(cli, ["status", "webapp"])
    assert one.exit_code == 0
    assert "new rows: (no baseline — no run record carries a measurement)" in one.output


def test_new_rows_stay_off_the_fleet_table(tmp_path: Path, caobunga_home: Path) -> None:
    """The coverage cell already counts a new unmarked row among `denied`.

    Its affix run is what the report cannot vouch for; a row it can name is not
    that, so the fleet view is deliberately unchanged.
    """
    repo = _flaitron_repo(tmp_path, DRIFTED_PLAN)
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo)]).exit_code == 0
    _new_rows_baseline(caobunga_home, [NewRow(task_id="CBN-014")])

    fleet = runner.invoke(cli, ["status"])
    assert fleet.exit_code == 0
    assert "new" not in fleet.output.lower()


def test_the_report_states_the_filing_condition_the_gate_refuses_on(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """Gate and report state the same fact, each in its own register (CBN-138.4).

    CBN-130 collapsed the two dirt implementations because a report that does not
    render what the gate refuses on is a report that lies about it. A label the
    gate prints and this omits re-opens that by the same argument — so the block
    states the condition, while only the refusal offers the command.
    """
    repo = _git_flaitron_repo(tmp_path, TREE_PLAN)
    (repo / ".flaitron" / "PLAN.md").write_text(
        TREE_PLAN + "\n- [ ] **CBN-099** [medium] | filed — filed mid-run.\n", encoding="utf-8"
    )
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo), "--tier", "proceed-on-green"]).exit_code == 0
    # A PLAN edit is a filing only after a run that filed rows (CBN-435).
    _new_rows_baseline(caobunga_home, [NewRow(task_id="CBN-099")])

    out = runner.invoke(cli, ["status", "webapp"])
    assert out.exit_code == 0
    assert "tree: dirty — 1 change(s) would refuse this dispatch (exit 2)" in out.output
    assert "every blocking line is a flaitron filing artifact" in out.output
    # The report only reports: the command belongs to the surface that stopped.
    # Scoped to the tree block itself (CBN-157 added an unrelated `git -C …
    # diff --stat` verification hint to the coverage seed block, which this
    # PLAN's own obvious `CBN-099` row now also triggers).
    tree_start = out.output.index("tree:")
    tree_end = out.output.index("last run:", tree_start)
    assert "git -C" not in out.output[tree_start:tree_end]


def test_the_report_reads_note_ownership_from_the_last_run(
    tmp_path: Path, caobunga_home: Path
) -> None:
    """The report's filing note follows the gate's owned set (CBN-434, CBN-130).

    A note becomes a filing once the last run's record names its id, here as a
    `new_rows` entry. The gate reads the same record, so the two cannot diverge.
    """
    repo = _git_flaitron_repo(tmp_path, TREE_PLAN)
    (repo / ".flaitron" / "tasknote" / "CBN-099.md").write_text(
        "---\nstatus: starter\n---\n", encoding="utf-8"
    )
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo), "--tier", "proceed-on-green"]).exit_code == 0

    before = runner.invoke(cli, ["status", "webapp"])
    assert before.exit_code == 0
    assert "filing artifact" not in before.output

    Store(caobunga_home).write_run(
        RunRecord(
            project="webapp",
            task_id="CBN-001",
            outcome=Outcome.BLOCKED,
            closed_at=utc_now(),
            summary="parked after filing",
            new_rows=[NewRow(task_id="CBN-099")],
        )
    )
    after = runner.invoke(cli, ["status", "webapp"])
    assert after.exit_code == 0
    assert "every blocking line is a flaitron filing artifact" in after.output


def test_operator_dirt_draws_no_filing_note(tmp_path: Path, caobunga_home: Path) -> None:  # noqa: ARG001
    """The affix must not leak onto a tree the predicate did not clear."""
    repo = _git_flaitron_repo(tmp_path, TREE_PLAN)
    (repo / "scratch.txt").write_text("operator edit\n", encoding="utf-8")
    runner = _runner()
    assert runner.invoke(cli, ["register", str(repo), "--tier", "proceed-on-green"]).exit_code == 0

    out = runner.invoke(cli, ["status", "webapp"])
    assert out.exit_code == 0
    assert "tree: dirty" in out.output
    assert "filing artifact" not in out.output


def test_run_red_suite_keeps_exit_zero_but_exposes_unsuccessful_receipt(
    tmp_path: Path,
    caobunga_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from conftest import (
        OPEN_PLAN,
        FakeWorker,
        _close_worker,
        _closing_client,
        _git_repo,
        _not_green_suite,
        _register,
        _usage,
    )

    repo = _git_repo(tmp_path, OPEN_PLAN)
    store = Store(caobunga_home)
    _register(store, repo)
    worker = _closing_client(FakeWorker(), lambda _msg: _close_worker(repo))
    execute = cli_module.execute_run

    def injected(project, **kwargs):
        return execute(
            project,
            store=store,
            worker=worker,
            usage=_usage(),
            run_suite=_not_green_suite("red"),
            poll_interval=0,
            **kwargs,
        )

    monkeypatch.setattr(cli_module, "execute_run", injected)
    result = _runner().invoke(cli, ["run", "app"])
    assert result.exit_code == 0, result.output
    assert result.output.count("run:\n") == 1
    assert "outcome: closed" in result.output
    assert "invocation: incomplete — suite; successful=false" in result.output
    assert "invocation-record:" in result.output and "recovery:" in result.output
    (path,) = (caobunga_home / "invocations").glob("*.yaml")
    receipt = store.load_invocation(path.stem)
    run = store.last_run("app")
    assert run is not None
    assert not receipt.successful and receipt.last_commit_sha == run.commit_sha
