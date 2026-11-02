"""Click entrypoint: register / projects / status / run / fleet / digest."""

from __future__ import annotations

import math
import shlex
from collections import Counter
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from threading import Lock
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from caobunga import __version__, install_provenance, pins
from caobunga.digest import format_digest
from caobunga.echo_style import run_echo
from caobunga.errors import ReceiptUnwritten, ReportError, UserError
from caobunga.flaitron import PARK_UNCOMMITTED, marker_drift, park_commits, try_read_project
from caobunga.fleet import DEFAULT_JOBS, fleet_exit, run_fleet
from caobunga.invocation import running_verdicts, stale_receipts
from caobunga.loop import execute_run
from caobunga.models import (
    ID_PATTERN,
    ExecutionTier,
    InvocationResult,
    Outcome,
    RegistryRecord,
    RetainedWorktree,
    RunRecord,
    utc_now,
)
from caobunga.presence import read_tree
from caobunga.recipes import hooks_available
from caobunga.retained import report_retained, retained_worktrees
from caobunga.status_render import (
    coverage_cell,
    dump_run_record,
    format_coverage,
    format_drift,
    format_fleet_seed,
    format_new_rows,
    format_pins,
    format_receipts,
    format_retained,
    format_seed_block,
    format_tree,
    outcome_cell,
    retained_footer,
    review_cell,
    stale_lines,
    suite_cell,
    tree_cell,
)
from caobunga.store import Store
from caobunga.suite_verbs import probe_fitness
from caobunga.usage import V1_PROVIDERS

# Headless floor so fleet-table cells stay one line. Rich's no-tty default
# is 80 columns, which wrapped the `verdict` column (CBN-160). A dumb tty
# (TERM=dumb or unknown, and is_terminal — FORCE_COLOR counts) ignores an
# explicit width unless height is set too, and returns 80x25 instead.
# 25 is Rich's own fallback row count (CBN-EPIC-362.2).
console = Console(width=200, height=25)
TIER_CHOICES = [tier.value for tier in ExecutionTier]
PROVIDER_TOKENS = [provider.value for provider in V1_PROVIDERS]


def _handle_user_error[F: Callable[..., Any]](func: F) -> F:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except UserError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(exc.exit_code) from exc

    return wrapper  # type: ignore[return-value]


def _store() -> Store:
    return Store()


_REINSTALL_HINT = "uv tool install --editable backend --force (from the caobunga repo root)"


def _source_line(*, with_reinstall_hint: bool) -> str:
    """`source:` provenance line for `--version` and `status` (CBN-169).

    A snapshot install silently shadows local fixes (CBN-154/155/156 were
    inert until an operator noticed and reinstalled by hand); `status`'s
    fleet-table context is where that reinstall command earns its keep, so
    only it carries the hint — `--version` reports the path alone.
    """
    kind, path = install_provenance()
    line = f"source: {kind} {path}"
    if with_reinstall_hint:
        # Fleet `status` is the stale-install surface (CBN-169); affix the
        # CBN-212 hook's presence so a snapshot missing `hooks.py` is visible
        # without another `worker-exited` (CBN-216). `--version` stays path-only.
        state = "present" if hooks_available() else "missing"
        line += f" hook: {state}"
    if with_reinstall_hint and kind == "snapshot":
        line += f" — reinstall: {_REINSTALL_HINT}"
    return line


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:  # noqa: ARG001
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"caobunga, version {__version__}")
    click.echo(_source_line(with_reinstall_hint=False))
    ctx.exit()


def resolve_register_path(raw: Path) -> Path:
    try:
        resolved = raw.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise UserError(f"path does not exist: {raw}") from exc
    if not resolved.is_dir():
        raise UserError(f"not a directory: {resolved}")
    return resolved


def parse_providers(raw: str | None) -> list[str] | None:
    """Comma-separated allow-list → ordered unique tokens. Unknown token is a user error."""
    if raw is None:
        return None
    tokens: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if token not in PROVIDER_TOKENS:
            raise UserError(f"unknown provider {token!r}: choose from {', '.join(PROVIDER_TOKENS)}")
        if token not in tokens:
            tokens.append(token)
    return tokens or None


def parse_bootstrap(raw: str | None) -> list[str] | None:
    """`--bootstrap` → the argv it names, split like a shell would but never run by one."""
    if raw is None:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise UserError(f"cannot parse --bootstrap {raw!r}: {exc}") from exc
    if not argv:
        raise UserError("empty --bootstrap; use --clear-bootstrap to remove one")
    return argv


def derive_id(resolved: Path, explicit: str | None) -> str:
    if explicit is not None:
        candidate = explicit
    else:
        candidate = resolved.name.lower()
    if not ID_PATTERN.fullmatch(candidate):
        if explicit is None:
            raise UserError(f"basename {resolved.name!r} is not a legal id; pass --id")
        raise UserError(f"invalid id {candidate!r}: must match [a-z][a-z0-9-]*")
    return candidate


@click.group()
@click.option(
    "--version",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_print_version,
    help="Show the version and exit.",
)
def cli() -> None:
    """Multi-project Flaitron orchestrator."""


@cli.command()
@click.argument("path", type=click.Path(path_type=Path))
@click.option(
    "--id",
    "project_id",
    default=None,
    help="Registry slug (default: directory basename).",
)
@click.option(
    "--tier",
    type=click.Choice(TIER_CHOICES, case_sensitive=True),
    default=None,
    help="Execution tier. New record: gated. Re-register: omit = keep the stored tier.",
)
@click.option("--replace", is_flag=True, help="Overwrite an existing id at a different path.")
@click.option(
    "--provider",
    default=None,
    help="Pin worker (provider token). Omit = picker decides.",
)
@click.option(
    "--model",
    default=None,
    help="Pin runtime model. Requires --provider. Omit = taskmap.",
)
@click.option(
    "--providers",
    default=None,
    help="Comma-separated allow-list the picker may choose from, best-first. "
    "Conflicts with --provider. Omit = the global default (`caobunga providers`), "
    "else every installed worker is eligible.",
)
@click.option(
    "--clear-providers",
    is_flag=True,
    help="Drop this record's allow-list so it falls back to the global default.",
)
@click.option(
    "--bootstrap",
    default=None,
    help='Command a fresh execution worktree runs before its baseline suite, e.g. "just setup". '
    "Split like a shell would; never run through one. Stored and served in plain text: "
    "never put a secret in it.",
)
@click.option(
    "--bootstrap-timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Seconds the bootstrap may run. Requires --bootstrap. Omit = 600.",
)
@click.option("--clear-bootstrap", is_flag=True, help="Drop the bootstrap command and its timeout.")
@click.option(
    "--isolate/--no-isolate",
    default=None,
    help="Run each launched task in its own wt-<ID> worktree and land a green close by "
    "fast-forward. Omit = keep the stored setting (off for a new record).",
)
@_handle_user_error
def register(
    path: Path,
    project_id: str | None,
    tier: str | None,
    replace: bool,
    provider: str | None,
    model: str | None,
    providers: str | None,
    clear_providers: bool,
    bootstrap: str | None,
    bootstrap_timeout: float | None,
    clear_bootstrap: bool,
    isolate: bool | None,
) -> None:
    """Write a registry record. Default tier gated."""
    resolved = resolve_register_path(path)
    slug = derive_id(resolved, project_id)
    pin_provider = provider.strip() if provider else None
    pin_model = model.strip() if model else None
    allow_list = parse_providers(providers)
    if pin_model is not None and pin_provider is None:
        raise UserError("model override requires --provider")
    if pin_provider is not None and allow_list is not None:
        # A pin beats even an exhausted worker; an allow-list is a preference
        # inside an eligibility filter. Ranking one over the other would be a guess.
        raise UserError("--provider and --providers are mutually exclusive")
    if clear_providers and providers is not None:
        raise UserError("--providers and --clear-providers are mutually exclusive")
    if clear_bootstrap and (bootstrap is not None or bootstrap_timeout is not None):
        raise UserError("--clear-bootstrap conflicts with --bootstrap and --bootstrap-timeout")
    if bootstrap_timeout is not None and bootstrap is None:
        raise UserError("--bootstrap-timeout requires --bootstrap")
    if bootstrap_timeout is not None and not math.isfinite(bootstrap_timeout):
        raise UserError(
            f"--bootstrap-timeout must be a finite number of seconds, got {bootstrap_timeout}"
        )
    bootstrap_argv = parse_bootstrap(bootstrap)
    record = RegistryRecord(
        id=slug,
        path=str(resolved),
        tier=ExecutionTier(tier or ExecutionTier.GATED.value),
        provider=pin_provider,
        model=pin_model,
        providers=allow_list,
        registered_at=utc_now(),
        enabled=True,
        bootstrap=bootstrap_argv,
        bootstrap_timeout=bootstrap_timeout,
        isolate=isolate,
    )
    stored = _store().register(
        record,
        replace=replace,
        keep_tier=tier is None,
        clear_providers=clear_providers,
        clear_bootstrap=clear_bootstrap,
    )
    click.echo(f"registered {stored.id} → {stored.path} ({stored.tier.value})")
    click.echo(probe_fitness(resolved).render())
    if not park_commits(resolved):
        click.echo(PARK_UNCOMMITTED.format(id=stored.id))


@cli.command("providers")
@click.argument("order", required=False)
@click.option("--clear", is_flag=True, help="Remove the global default.")
@_handle_user_error
def providers_verb(order: str | None, clear: bool) -> None:
    """Show or set the global provider allow-list, best-first (CBN-414).

    Used by any record with neither --provider nor --providers. No argument:
    print it.
    """
    store = _store()
    if order is not None and clear:
        raise UserError("pass a provider list or --clear, not both")
    config = store.load_config()
    if order is not None or clear:
        allow_list = None if clear else parse_providers(order)
        if not clear and allow_list is None:
            raise UserError("empty provider list; use --clear to remove the default")
        config = store.save_config(config.model_copy(update={"providers": allow_list}))
    click.echo(f"providers: {', '.join(config.providers) if config.providers else 'none'}")


@cli.command()
@_handle_user_error
def projects() -> None:
    """List registered projects (id, path, tier, enabled)."""
    rows = _store().list_projects()
    table = Table(title="projects")
    table.add_column("id")
    table.add_column("path")
    table.add_column("tier")
    table.add_column("enabled")
    for row in rows:
        table.add_row(row.id, row.path, row.tier.value, str(row.enabled).lower())
    console.print(table)


@cli.command()
@click.argument("project", required=False)
@click.option(
    "--seed",
    is_flag=True,
    help="Fleet form only: every repo's obvious unmarked rows plus paste-ready sed blocks.",
)
@_handle_user_error
def status(project: str | None, seed: bool) -> None:
    """Read-only: last run record + tier + enabled. No arg: one row per project."""
    store = _store()
    if project is not None:
        if seed:
            raise UserError("--seed is the fleet form; `status <id>` already prints its seed block")
        _print_project_status(store, store.get(project))
        return
    click.echo(_source_line(with_reinstall_hint=True))
    rows = store.list_projects()
    if seed:
        reads = [(row, try_read_project(Path(row.path))) for row in rows]
        repos = [(row.id, row.path, None if read is None else read.coverage) for row, read in reads]
        for line in format_fleet_seed(repos):
            click.echo(line)
        return
    table = Table(title="status")
    table.add_column("id")
    table.add_column("tier")
    table.add_column("enabled")
    table.add_column("last_task")
    table.add_column("last_outcome")
    table.add_column("suite")
    table.add_column("review")
    table.add_column("coverage")
    table.add_column("tree")
    footers: list[str] = []
    for row in rows:
        last = store.last_run(row.id)
        retained = report_retained(row)
        read = try_read_project(Path(row.path), retained or ())
        drift = [] if read is None or last is None else marker_drift(read.tasks, last.marked_tasks)
        footers.extend(retained_footer(row, _retained_listing(store, row, retained)))
        table.add_row(
            row.id,
            row.tier.value,
            str(row.enabled).lower(),
            last.task_id if last else "—",
            outcome_cell(last),
            suite_cell(last),
            review_cell(last),
            "—" if read is None else coverage_cell(read.coverage, drift),
            "—"
            if read is None
            else tree_cell(
                read_tree(read, row.path, row.tier, last_run=last, retained=retained or ())
            ),
        )
    console.print(table)
    _echo_fleet_footers(store, rows, footers)


def _echo_fleet_footers(store: Store, rows: list[RegistryRecord], retained: list[str]) -> None:
    """The fleet table's footers: retained worktrees, then stale receipts."""
    _echo_lines(retained)
    by_project: dict[str, list[tuple[InvocationResult, bool | None]]] = {}
    for receipt, alive in running_verdicts(store, None):
        by_project.setdefault(receipt.project, []).append((receipt, alive))
    for row in rows:
        verdicts = by_project.get(row.id, [])
        _echo_lines(stale_lines(row, verdicts, stale_receipts(row, verdicts)))


def _echo_lines(lines: list[str]) -> None:
    for line in lines:
        click.echo(line)


def _retained_listing(
    store: Store, record: RegistryRecord, retained: frozenset[str] | None
) -> list[RetainedWorktree] | None:
    return retained_worktrees(record, retained, lambda: store.list_runs(record.id))


def _print_project_status(store: Store, record: RegistryRecord) -> None:
    click.echo(f"id: {record.id}")
    click.echo(f"path: {record.path}")
    click.echo(f"tier: {record.tier.value}")
    click.echo(f"enabled: {str(record.enabled).lower()}")
    for line in format_pins(pins.check_all()):
        click.echo(line)
    retained = report_retained(record)
    read = try_read_project(Path(record.path), retained or ())
    last = store.last_run(record.id)
    if read is None:
        click.echo("coverage: (unreadable)")
    else:
        for line in format_coverage(read.coverage):
            click.echo(line)
        for line in format_seed_block(read.coverage, record.path):
            click.echo(line)
        drift = [] if last is None else marker_drift(read.tasks, last.marked_tasks)
        for line in format_drift(drift, last):
            click.echo(line)
        for line in format_new_rows(last):
            click.echo(line)
        tree = read_tree(read, record.path, record.tier, last_run=last, retained=retained or ())
        for line in format_tree(tree):
            click.echo(line)
    _echo_lines(format_retained(record, _retained_listing(store, record, retained)))
    verdicts = running_verdicts(store, record.id)
    for line in format_receipts(record, verdicts, stale_receipts(record, verdicts)):
        click.echo(line)
    if last is None:
        click.echo("last run: (none)")
        return
    click.echo("last run:")
    click.echo(dump_run_record(last), nl=False)


@cli.command("run")
@click.argument("project")
@_handle_user_error
def run(project: str) -> None:
    """Honor that project's tier. observe: status only. gated: one task then stop.

    proceed-on-green: drain the queue until nothing is eligible. Every launching
    tier requires the `[unattended]` marker (CBN-118) and refuses a dirty target
    tree; escalation goes to the operator on failure at every tier.
    """
    # Track the last record dumped by identity, not a bool: a later-tier run writes
    # one record per close, and a ReportError on close N carries a record no earlier
    # dump covered. A bare flag hid every close after the first (CBN-EPIC-9.5 F3).
    dumped: RunRecord | None = None
    # TTY-only emoji + color; piped output stays byte-identical (CBN-EPIC-449.4).
    echo = run_echo()

    def on_run(record: RunRecord) -> None:
        nonlocal dumped
        if dumped is record:
            return
        dumped = record
        echo("run:")
        click.echo(dump_run_record(record), nl=False)

    try:
        record = execute_run(project, echo=echo, on_run=on_run)
    except ReportError as exc:
        on_run(exc.record)
        raise
    except ReceiptUnwritten as exc:
        # The receipt is gone; the last run record is what says what committed (CBN-442).
        if exc.record is not None:
            on_run(exc.record)
        raise
    if record is None:
        return
    on_run(record)
    if record.outcome is Outcome.FAILED:
        raise SystemExit(4)


@cli.command()
@click.option(
    "--jobs",
    type=click.IntRange(min=1),
    default=DEFAULT_JOBS,
    show_default=True,
    help="Most projects running at once.",
)
@_handle_user_error
def fleet(jobs: int) -> None:
    """`run` every enabled project, at most --jobs at once; one receipt after.

    Each project is a child `caobunga run <slug>`, its output prefixed
    `[slug]`. One project's stop never halts another; exit 2 / 3 are skips.
    """
    slugs = [row.id for row in _store().list_projects() if row.enabled]
    if not slugs:
        click.echo("fleet: no enabled projects")
        return
    lock = Lock()

    def echo(line: str) -> None:
        with lock:
            click.echo(line)

    click.echo(f"fleet: {len(slugs)} projects, jobs {jobs}")
    legs = run_fleet(slugs, jobs, echo=echo)
    table = Table(title="fleet")
    table.add_column("id")
    table.add_column("exit")
    table.add_column("meaning")
    table.add_column("duration")
    for leg in legs:
        table.add_row(leg.slug, str(leg.exit_code), leg.meaning, f"{leg.seconds:.0f}s")
    console.print(table)
    tally = Counter(leg.meaning for leg in legs)
    click.echo("fleet: " + " · ".join(f"{count} {meaning}" for meaning, count in tally.items()))
    code = fleet_exit(legs)
    if code:
        raise SystemExit(code)


@cli.command()
@_handle_user_error
def digest() -> None:
    """Daily fleet rollup over run records, then the pin-freshness block."""
    click.echo(format_digest(_store()), nl=False)
    # CLI-only (CBN-256): the live catalog probe stays out of `format_digest`,
    # which `GET /api/digest` also serves as a pure run-record read.
    click.echo("\n## Model pins\n")
    for line in format_pins(pins.check_all()):
        click.echo(f"- {line}")
