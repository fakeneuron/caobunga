"""Text renderers for the `status` and `digest` verbs.

`caobunga.status_render` owns how a project's state reads in a terminal: the
fleet-table cells, the detail-form blocks (coverage, seed, drift, new rows,
tree, pins), and the run-record dump. A leaf module: `models`, the flaitron
façade, `presence`, and `pins` cross in, and nothing calls back into `cli.py`,
which keeps the click commands and the echo/store orchestration around these.
Split out of `cli.py` at CBN-239.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

import yaml

from caobunga import pins
from caobunga.flaitron import PLAN_REL, Coverage, Denial, TriageClass
from caobunga.models import RunRecord, iso_z
from caobunga.presence import TreeState

# The seed block's commit hint carries a message so pasting it opens no editor (CBN-355).
_SEED_COMMIT_MSG = "chore: seed [unattended] markers"


def dump_run_record(record: RunRecord) -> str:
    """Human-readable YAML of a run record. Omits unset optional fields."""
    return yaml.safe_dump(record.model_dump(mode="json", exclude_none=True), sort_keys=False)


def outcome_cell(last: RunRecord | None) -> str:
    """Fleet-table form: the last run's outcome, with its park reason appended when set.

    A `blocked` outcome carries no further detail on its own, so every parked
    project reads identically in this column regardless of why it stopped — a
    `visual-confirm` park (needs a human to look at something) sits
    indistinguishable from a routine `dependency` park in the same table
    (CBN-187). Appending the code follows `coverage_cell`'s
    parenthetical-affix idiom rather than adding a column.
    """
    if last is None:
        return "—"
    if last.park_reason:
        return f"{last.outcome.value} ({last.park_reason})"
    return last.outcome.value


def suite_cell(last: RunRecord | None) -> str:
    """Fleet-table form: the last run's deterministic suite verdict, or `—`.

    Dash idiom for "no last run" and "no suite on it" alike (a v1 record, or a
    non-closure outcome that never measures one) — one dash rule across the
    structured columns (CBN-EPIC-202.5).
    """
    if last is None or last.suite is None:
        return "—"
    return last.suite.verdict.value


def review_cell(last: RunRecord | None) -> str:
    """Fleet-table form: how the last run's External review box closed, or `—`.

    Same dash idiom as `suite_cell` for "no last run" and "not read" alike (a
    park, a `no-tasknote` close, or a record written before CBN-257).
    """
    if last is None or last.external_review is None:
        return "—"
    return last.external_review.value


def coverage_cell(coverage: Coverage, drift: Sequence[Denial] = ()) -> str:
    """Fleet-table form: `marked/open`, flagging obvious, dropped, and drifted rows.

    All three affixes appear only when nonzero — the common clean repo keeps the
    compact cell CBN-EPIC-18.3 shipped, and a repo with only dropped rows keeps
    the exact `(1 unparsed)` affix CBN-115 shipped. They share one parenthetical
    rather than stacking three, because they are three counts of the same thing:
    how much of this repo's queue the report has something to say about. `obvious`
    (CBN-157) leads the list when a row was confirmed. A clause 3 keyword miss
    does not set it (CBN-361), so a real read leaves that affix at zero.
    """
    cell = f"{coverage.marked}/{coverage.open}"
    affixes = []
    obvious = sum(1 for denial in coverage.denials if denial.triage is TriageClass.OBVIOUS)
    if obvious:
        affixes.append(f"{obvious} obvious")
    if coverage.unparsed:
        affixes.append(f"{coverage.unparsed} unparsed")
    if drift:
        affixes.append(f"{len(drift)} drifted")
    if affixes:
        cell += f" ({', '.join(affixes)})"
    return cell


# Wide enough to clear the decoration run — `- [ ] **DEPLOY-13.5** [!critical]
# [light]🔧 [unattended] | shortname` is ~96 — so the seeded marker is always
# inside the budget, whatever the description does after it (CBN-119.3).
_ROW_BUDGET = 96


def _elide(row: str) -> str:
    """A PLAN row trimmed to `_ROW_BUDGET`, marked when anything was cut."""
    text = row.strip()
    return text if len(text) <= _ROW_BUDGET else f"{text[:_ROW_BUDGET]}…"


def tree_cell(state: TreeState) -> str:
    """Fleet-table form: `clean`, `dirty (N)`, or `unreadable`.

    The column exists because the fleet table is the "which repos are ready"
    view and, before CBN-130, it could report full marker coverage on a repo
    whose next launch would refuse at exit 2. The count, not a flag, for the
    same reason the coverage cell counts: the operator commits or stashes a
    countable number of files.
    """
    if not state.readable:
        return "unreadable"
    if not state.blocking:
        return "clean"
    return f"dirty ({len(state.blocking)})"


def format_tree(state: TreeState) -> list[str]:
    """Detail-form tree state: the verdict, then what an operator would act on.

    **Always emits a line**, on CBN-125's rule — a guard that renders nothing
    when it cannot read git is indistinguishable from one that checked and found
    the tree clean. Three states, all stated (`clean` / `dirty` / `unreadable`).

    A dirty block names the candidate whose exemption was applied, then each
    blocking line. **The classification is the point (CBN-130).** The exemption
    is scoped to the candidate's own tasknote, so an open tasknote for any
    *other* task is dirt because of which row is the candidate — two decisions
    the operator makes separately, coupled with nothing showing it until a launch
    refused. Labeling the line says so, and `clean_for` closes it: when one open
    tasknote is the only thing blocking, the block states which dispatch the gate
    would read clean for.

    `filing_only` renders here for the same reason (CBN-138.4). The gate's own
    refusal names a filing and offers a commit, and a label the gate prints that
    this report does not render is precisely the gate/report divergence CBN-130
    closed — so the two surfaces state the same fact, each in its own register:
    the refusal offers the command because it is the surface that stopped, and
    this one states the condition because it is the surface that only reports.
    """
    if not state.readable:
        return ["tree: unreadable — git could not report; a launch would refuse (exit 2)"]
    candidate = state.candidate or "(none eligible)"
    if not state.blocking:
        return [f"tree: clean — nothing would refuse a dispatch of {candidate}"]
    lines = [
        f"tree: dirty — {len(state.blocking)} change(s) would refuse this dispatch (exit 2)",
        f"  candidate: {candidate} ({_exempt_note(state)})",
    ]
    for entry in state.blocking:
        lines.append(f"  {entry.text.strip()}")
        if entry.task_id is not None:
            lines.append(
                f"    open tasknote for {entry.task_id}; "
                f"dirt only because the candidate is {candidate}"
            )
    if state.clean_for is not None:
        lines.append(f"  note: the gate reads clean only for a dispatch of {state.clean_for}")
    if state.filing_only:
        lines.append(
            "  note: every blocking line is a flaitron filing artifact — "
            "likely a worker's filing, not operator dirt"
        )
    return lines


def _exempt_note(state: TreeState) -> str:
    if state.exempt_task is None:
        return "no open tasknote — nothing exempt"
    return f"open tasknote {state.exempt_task} exempt"


def format_coverage(coverage: Coverage) -> list[str]:
    """Detail-form coverage: the summary line, then a block per denied task.

    Names the ids rather than only tallying them (CBN-EPIC-18.3): the operator
    seeds `[unattended]` markers against PLAN lines, so a count alone would
    still send them to the target repo to find out which ones. Unparsed rows
    (CBN-115) follow the same rule with the only handle they have — the PLAN
    line number and its raw text — and appear only when any exist.

    Each denial then carries what it takes to *act* on it (CBN-119.3): the PLAN
    line number, the row as it reads now, and — when a marker is the fix — the
    row as it should read, with `[unattended]` placed after `[model]` and any
    suggestion glyph (CBN-343). Denials print in dispatch order, so working down the
    list is the seeding order. A denial with no `seeded` line prints one row and
    no second: either seeding is not the fix (a `blocked`, `exploratory`,
    `frontier`, `handoff` or `parked` denial), or no safe insertion exists and the
    operator edits by hand
    (`flaitron.seeded_line`).

    **Both rows are elided to `_ROW_BUDGET`.** The `Denial` carries the complete
    line — the API and the dashboard render it whole — but a terminal does not:
    a real PLAN description runs to several hundred characters, and printed in
    full the two rows wrap into a wall whose only difference is thirteen
    identical-looking characters near the start. The edit always lands inside
    the decoration run, which is always at the head, so eliding the tail is what
    makes the placement *visible* rather than merely present.

    **`unmarked` denials group under an `obvious` heading first (CBN-157).**
    Every other denial code (`blocked`, `exploratory`, `frontier`, `handoff`, `parked`) keeps
    `Coverage.denials`'s own dispatch order with no heading, exactly as before
    this task — the mirrored predicate never judged them, so grouping them
    under "needs judgment" would claim a verdict this report never reached.
    The heading only appears once at least one `unmarked` row triages
    `obvious`; a repo with none keeps the flat list CBN-EPIC-18.3 shipped.
    """
    summary = (
        f"coverage: {coverage.marked} marked / {coverage.denied} denied of {coverage.open} open"
    )
    if coverage.unparsed:
        summary += f" · {coverage.unparsed} unparsed"
    lines = [summary]
    obvious = [d for d in coverage.denials if d.triage is TriageClass.OBVIOUS]
    if obvious:
        rest = [d for d in coverage.denials if d.triage is not TriageClass.OBVIOUS]
        lines.append(f"  obvious ({len(obvious)}) — mirrored predicate admits, a marker suffices:")
        for denial in obvious:
            lines.extend(_denial_lines(denial))
        if rest:
            lines.append(f"  needs judgment ({len(rest)}):")
            for denial in rest:
                lines.extend(_denial_lines(denial))
    else:
        for denial in coverage.denials:
            lines.extend(_denial_lines(denial))
    lines.extend(f"  line {row.line}  unparsed: {row.text}" for row in coverage.unparsed_rows)
    return lines


def _denial_lines(denial: Denial) -> list[str]:
    """One denial rendered as `deny:` plus the `triage:` / `now:` / `warn:` / `seed:` rows.

    Extracted from `format_coverage` when the drift block (CBN-125) needed the
    identical shape: the two blocks answer different questions — which rows to
    seed, versus which rows lost an arming they already had — but a reader acts
    on both the same way, from the same lines. Adding a row here reaches all
    three denial lists at once, which is why it is added here.

    **`warn:` sits between `now:` and `seed:` on purpose (CBN-128).** It is the
    caution, and `seed:` is the thing it cautions against — a paste-ready line
    that makes an apparently-unblocked row dispatch. This is the operator's
    decision point: once seeded, the row is marked and leaves this list. Both
    rows are printed, because the warning is a claim about *prose* and the marker
    genuinely is what an `unmarked` row needs; suppressing the fix on a reading
    of someone else's sentence is the boundary `unparsed_blocked_clause` declines
    to cross.

    **`triage:` sits right after `deny:` (CBN-157).** It explains *why* a
    `needs-judgment` row didn't triage `obvious` before the reader reaches the
    unrelated `warn:`/`seed:` machinery; an `obvious` row prints no `triage:`
    line at all — the heading it sits under already says so.

    Eliding `warn:` at the same budget spends it on the clause rather than the
    decoration run: the fragment already starts at the mention, so what survives
    here is exactly the tail the other two rows cut.
    """
    where = f"line {denial.line}  " if denial.line else ""
    lines = [f"  {where}{denial.task_id}  deny: {denial.reason}"]
    if denial.text:
        lines.append(f"    now:  {_elide(denial.text)}")
    if denial.triage is TriageClass.NEEDS_JUDGMENT and denial.triage_reason is not None:
        lines.append(f"    triage: {_elide(denial.triage_reason)}")
    if denial.unparsed_blocker is not None:
        lines.append('    warn: says "blocked", no `Blocked by [[ID]]` clause parsed')
        lines.append(f"          {_elide(denial.unparsed_blocker)}")
    if denial.seeded_line is not None:
        lines.append(f"    seed: {_elide(denial.seeded_line)}")
    return lines


# `sed`-pattern vs `sed`-replacement escaping differ (CBN-157): a pattern needs
# every BRE metacharacter escaped, a replacement only `\`, `&`, and the shared
# `/` delimiter. Order matters within each class — `\\` first, so the escaping
# backslash added for a later character is never itself re-escaped.
_SED_PATTERN_SPECIAL = re.compile(r"[.\\*^$\[\]/]")
_SED_REPLACEMENT_SPECIAL = re.compile(r"[\\&/]")


def _sed_pattern(text: str) -> str:
    return _SED_PATTERN_SPECIAL.sub(lambda m: "\\" + m.group(0), text)


def _sed_replacement(text: str) -> str:
    return _SED_REPLACEMENT_SPECIAL.sub(lambda m: "\\" + m.group(0), text)


def _seed_edit(before: str, after: str) -> tuple[str, str] | None:
    """`(pattern, replacement)` literal strings for a `sed` edit turning `before`
    into `after`, given `after` is `before` with one contiguous insertion
    (CBN-157) — true of every `Denial.text` / `seeded_line` pair by
    construction (`flaitron.seeded_line` only ever inserts `[unattended]`).

    Anchors on the two strings' longest common prefix rather than reusing
    flaitron's own insertion-point logic, so this needs no `PlanTask` — a
    `Denial` already carries both plain strings. The replacement is `after`'s
    own prefix through the inserted span, **not** `pattern + " [unattended]"`:
    the common prefix can run a few characters past the true insertion point
    when the row's own text happens to start the same way (a shared leading
    space, or another bracket token right after `[model]`), and only slicing
    `after` at the corresponding length reconstructs it correctly in every
    case rather than just the common one.

    The prefix always includes the full `**task_id**` token, because nothing
    before `[unattended]`'s insertion point can differ between `before` and
    `after` — so the pattern is unique in a well-formed PLAN.md without ever
    anchoring on line start, which is what makes it indentation-proof (an
    indented epic child seeds exactly like a top-level row).
    """
    prefix_len = 0
    limit = min(len(before), len(after))
    while prefix_len < limit and before[prefix_len] == after[prefix_len]:
        prefix_len += 1
    if prefix_len == 0:
        return None
    inserted_len = len(after) - len(before)
    if inserted_len <= 0:
        return None
    return before[:prefix_len], after[: prefix_len + inserted_len]


def _seed_command_lines(denial: Denial, plan: str = PLAN_REL.as_posix()) -> list[str]:
    """One idempotent `sed` edit for `denial`, or `[]` when it can't build one.

    `plan` is the PLAN path the edit names: relative for the detail block, a
    repo's absolute path for the fleet worklist (CBN-348). Shell-quoted, which
    leaves the relative default byte-identical.

    `sed -i.bak SCRIPT file && rm -f file.bak` rather than BSD's `sed -i ''
    SCRIPT file`: the attached-suffix form parses identically on BSD (macOS)
    and GNU (Linux) sed, while the space-separated empty-suffix form is BSD-only
    — GNU's `-i` takes no detached argument, so it reads the empty string as
    the script and the real script as a filename it then can't find. An
    operator's own shell is usually one or the other; CI (Linux) caught this
    the first time the block was actually executed outside a macOS terminal.
    """
    if denial.seeded_line is None:
        return []
    edit = _seed_edit(denial.text, denial.seeded_line)
    if edit is None:
        return []
    pattern, replacement = edit
    script = f"s/{_sed_pattern(pattern)}/{_sed_replacement(replacement)}/"
    target, backup = shlex.quote(plan), shlex.quote(f"{plan}.bak")
    return [
        f"  grep -qF -- {shlex.quote(replacement)} {target} || \\",
        f"    sed -i.bak {shlex.quote(script)} {target} && rm -f {backup}",
    ]


def format_seed_block(coverage: Coverage, path: str) -> list[str]:
    """One paste-ready, idempotent shell block seeding every `unmarked` fix (CBN-157).

    The `seed:` row inside each denial (`_denial_lines`) is elided to
    `_ROW_BUDGET` and its leading whitespace is not reproduced there, so it
    names the fix but cannot be pasted — and hides that an indented epic child
    needs its indentation preserved too. The first hand-written `sed` in the
    fleet-wide seeding pass this task is filed from anchored at column 0 and
    missed 7 of 11 rows, every one an indented child. Each line here is built
    from `Denial.text` / `Denial.seeded_line` instead (`_seed_edit`), which
    needs no line-start anchor at all — indentation never enters into the match.

    **Idempotent by construction.** A `grep -qF` guard checks whether the
    row already reads as seeded before ever invoking `sed`, so pasting the
    block twice edits the file once — one fleet repo's lesson (`d1d14d9a`): a
    bare `[model]`-anchored `sed` with no such guard re-matched on re-run and
    doubled the token.

    **Path-scoped, never a broad commit.** The trailing comment reminds the
    operator to scope their own commit to `.flaitron/PLAN.md` — the same pass
    also found a `git commit -am` sweep an unrelated session's uncommitted
    work product into the seed commit.

    Nothing here runs. It is text to paste into a shell already rooted at
    `path`; Caobunga writes nothing into a target repo either way
    (`docs/CONTRACT.md` §"Marker coverage" — read-only in both directions).
    """
    edits = [line for denial in coverage.denials for line in _seed_command_lines(denial)]
    if not edits:
        return []
    lines = [f"  seed — paste into a shell rooted at {path}:"]
    lines.extend(edits)
    lines.append(f"  # then: git -C {shlex.quote(path)} diff --stat")
    lines.append(
        f"  # commit scoped to the edit: "
        f'git -C {shlex.quote(path)} commit -m "{_SEED_COMMIT_MSG}" '
        f"-- {PLAN_REL.as_posix()}"
    )
    return lines


def format_fleet_seed(repos: Sequence[tuple[str, str, Coverage | None]]) -> list[str]:
    """`status --seed`: every repo's `obvious` unmarked rows as one worklist (CBN-348).

    `repos` is `(id, path, coverage)` per registered project, `None` coverage
    for an unreadable path. Only `obvious` rows appear. The detail block seeds
    both triage classes (CBN-157), but a fleet-wide pass is the one place a
    reader could paste dozens of edits unread, so it lists only the rows the
    mirrored predicate already cleared. A clause 3 keyword miss is not cleared
    (CBN-361), so it never reaches this list. `needs judgment` stays per repo,
    in `status <id>`.

    Each repo's edits name its PLAN by absolute path, so every repo's block
    pastes from one shell anywhere instead of one `cd` per repo. The commit
    stays per repo and path-scoped, exactly as the detail block's comment says.
    An obvious row with no buildable edit says so rather than going quiet.

    **Always emits the headline**, counting only the repos actually read, and
    names unreadable ones (CBN-125's rule): "nothing to seed" and "could not
    read" must not look alike.
    """
    found = [
        (project, path, obvious)
        for project, path, coverage in repos
        if coverage is not None
        and (obvious := [d for d in coverage.denials if d.triage is TriageClass.OBVIOUS])
    ]
    count = sum(len(obvious) for _, _, obvious in found)
    unreadable = [project for project, _, coverage in repos if coverage is None]
    read = len(repos) - len(unreadable)
    if found:
        lines = [
            f"seed worklist: {count} obvious row(s) across {len(found)} of {read} read repo(s)"
        ]
    else:
        lines = [f"seed worklist: none — no obvious unmarked row across {read} read repo(s)"]
    if unreadable:
        lines.append(f"  unreadable (not checked): {', '.join(unreadable)}")
    for project, path, obvious in found:
        plan = f"{path}/{PLAN_REL.as_posix()}"
        lines.extend(["", f"{project}  ({path})"])
        for denial in obvious:
            lines.extend(_denial_lines(denial))
        edits = [line for denial in obvious for line in _seed_command_lines(denial, plan)]
        if not edits:
            lines.append("  seed — no paste-ready edit; seed these rows by hand")
        else:
            lines.append("  seed — paste from any shell:")
            lines.extend(edits)
            lines.append(f"  # then: git -C {shlex.quote(path)} diff --stat")
            lines.append(
                f"  # commit scoped to the edit: "
                f'git -C {shlex.quote(path)} commit -m "{_SEED_COMMIT_MSG}" '
                f"-- {PLAN_REL.as_posix()}"
            )
    return lines


def format_drift(drift: Sequence[Denial], baseline: RunRecord | None) -> list[str]:
    """Detail-form marker drift: one headline, then a block per drifted row.

    **Always emits a line**, even when there is nothing to report (CBN-125). A
    guard that prints nothing because it has no baseline is indistinguishable
    from a guard that checked and found the cohort intact, and that silence is
    the blind-spot class this repo keeps closing — CBN-115 for grammar-dropped
    rows, CBN-EPIC-17.2 for eligibility. Three states, all stated: rows drifted,
    nothing drifted, or no run record carries a marked set to diff against.

    The baseline is named by the run that recorded it, so the operator can see
    *how old* the comparison is — a diff against a run from three weeks ago says
    something different from one against this morning's.
    """
    if baseline is None or not baseline.marked_tasks:
        return ["drift: (no baseline — no run record carries a marked set)"]
    since = f"{baseline.task_id}, {iso_z(baseline.closed_at)}"
    armed = len(baseline.marked_tasks)
    if not drift:
        return [f"drift: none — all {armed} row(s) marked at the last run ({since}) still dispatch"]
    lines = [
        f"drift: {len(drift)} of {armed} row(s) marked at the last run ({since}) no longer dispatch"
    ]
    for denial in drift:
        lines.extend(_denial_lines(denial))
    return lines


def format_new_rows(baseline: RunRecord | None) -> list[str]:
    """Detail-form new-row report: the headline, then one line per row (CBN-138.3).

    **Always emits a line**, on CBN-125's rule — a report that renders nothing
    when the last run recorded no rows is indistinguishable from one that had no
    run to read, and that silence is the blind-spot class this repo keeps closing.
    Three states, all stated: rows appeared, none appeared, or no run record
    carries a measurement at all.

    Read straight off the record rather than re-diffed live: the claim is what one
    past run produced, and it is dated by the run that produced it — the same way
    `format_drift` names its baseline, so the operator can see how old the answer
    is. A row seeded since still prints as it was observed; `coverage:` above is
    the surface that answers the current question.

    The fleet table is deliberately untouched. A new unmarked row already lands in
    the coverage cell's `denied` count, and that cell's affix run is documented as
    what the report *cannot vouch for* — a row it can see and name is not that.
    """
    if baseline is None:
        return ["new rows: (no baseline — no run record carries a measurement)"]
    since = f"{baseline.task_id}, {iso_z(baseline.closed_at)}"
    rows = baseline.new_rows
    if not rows:
        return [f"new rows: none filed during the last run ({since})"]
    unmarked = sum(1 for row in rows if not row.unattended)
    lines = [f"new rows: {len(rows)} filed during the last run ({since}), {unmarked} unmarked"]
    for row in rows:
        state = "marked" if row.unattended else "unmarked — seed [unattended] to dispatch"
        lines.append(f"  {row.task_id}  {state}")
    return lines


def format_pins(reports: Sequence[pins.PinReport]) -> list[str]:
    """Detail-form pin-freshness: one summary line, then one per notable provider.

    Read-only, never a refusal and never a re-pin (CBN-166) — the same posture
    `format_coverage` / `format_drift` already hold for this fleet. Scoped to
    *installed* providers only, per the task's own filing: an uninstalled
    worker's pin freshness is not this operator's problem today. **Always
    emits at least the summary line** (CBN-125's rule) so a report that found
    nothing to flag reads as "checked, clean" rather than as silence.

    A provider whose catalog is declared partial (codex, per CBN-165) never
    contributes a `not in catalog` line — its absence proves nothing — and
    instead states the partial-catalog caveat once, so the operator does not
    mistake silence there for a verified-fresh pin either.
    """
    installed = [report for report in reports if report.installed]
    if not installed:
        return ["pins: (no installed provider)"]
    lines = [f"pins: {len(installed)} installed provider(s) checked"]
    for report in installed:
        if report.stale:
            lines.extend(
                f"pins: {report.provider} {model_id} not in catalog" for model_id in report.stale
            )
        elif report.partial:
            lines.append(f"pins: {report.provider} catalog partial (TAP-gated ids hidden)")
        elif report.message:
            lines.append(f"pins: {report.provider} {report.message}")
    return lines
