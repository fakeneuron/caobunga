"""TTY styling seam for `caobunga run` echo lines (CBN-EPIC-449.4)."""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from caobunga import echo_style
from caobunga.cli import cli
from caobunga.echo_style import STYLES, run_echo, should_style, style_line, tone_of
from caobunga.models import InvocationStatus, StopReason, SuiteVerdict

LINES = [
    "launch: CBN-1 · provider claude · model opus · tier gated",
    "phase: 2 execution · 1m02s",
    "progress: 3m00s elapsed · phase 2 execution · 4 tool calls · tool: Bash ls",
    "eligibility: CBN-1 allow: marked",
    "drain: complete — no eligible candidate remains",
    "unknown-word: left alone",
    "no prefix here",
]


@pytest.mark.parametrize(
    ("line", "tone"),
    [
        ("launch: CBN-1 · provider claude", "ok"),
        ("phase: 1 discovery · 0m05s", "info"),
        ("progress: 1m00s elapsed · phase none yet", "wait"),
        ("eligibility: CBN-1 allow: marked", "ok"),
        ("eligibility: CBN-1 deny: unmarked", "wait"),
        ("eligibility: CBN-1 allow withdrawn: dirty — not launching", "stop"),
        ("bootstrap: ok (1.2s)", "ok"),
        ("bootstrap: uv sync exit 1 (3.0s)", "stop"),
        ("suite: green — test 0 1.0s", "ok"),
        ("suite: red — test 1 1.0s", "stop"),
        ("suite: waiting for suite.lock", "wait"),
        ("baseline: main moved to abc since its last green suite", "wait"),
        ("landing: wt-CBN-1 fast-forwarded /repo to abc", "ok"),
        ("landing: wt-CBN-1 not landed — diverged", "stop"),
        ("landing: submodules not updated — core", "wait"),
        ("landing: worktree /wt not removed — busy", "wait"),
        ("drain: complete — no eligible candidate remains", "ok"),
        ("drain: incomplete — CBN-2 (unmarked)", "wait"),
        ("drain: suite — CBN-1 suite red", "stop"),
        ("drain: outcome — CBN-1 outcome blocked (input-needed)", "wait"),
        ("drain: outcome — CBN-1 outcome de-scoped", "wait"),
        ("drain: outcome — CBN-1 outcome failed", "stop"),
        ("invocation: complete — complete; successful=true", "ok"),
        ("invocation: incomplete — incomplete; successful=true", "wait"),
        ("invocation: empty — empty; successful=true", "info"),
        ("invocation: refused — dirty-tree; successful=false", "stop"),
        ("usage-limit: claude exhausted this invocation; re-picking", "wait"),
        ("usage-limit: re-pick codex cannot launch: boom", "stop"),
        ("stale-receipt: r1 running since 2026-11-01T00:00:00", "wait"),
        ("stale-receipt: r1 not rewritten: disk full", "stop"),
        ("parked: CBN-1 input-needed", "wait"),
        ("card: failed: disk full", "stop"),
        ("card: /x/status.md", "info"),
        ("rollup: skipped (PLAN unreadable)", "wait"),
        ("retained: unreadable — git could not list", "stop"),
    ],
)
def test_tone_by_prefix_and_verdict(line: str, tone: str) -> None:
    prefix, rest = line.split(":", 1)
    assert tone_of(prefix, rest) == tone


def test_unknown_prefix_has_no_tone() -> None:
    assert tone_of("tool", " Bash ls") is None


def test_style_line_prefixes_emoji_and_colors_the_word_only() -> None:
    styled = style_line("landing: wt-CBN-1 not landed — diverged")
    assert (
        styled
        == "🛬 " + click.style("landing:", fg="red", bold=True) + " wt-CBN-1 not landed — diverged"
    )


def test_info_lines_get_emoji_without_color() -> None:
    assert style_line("phase: 2 execution · 1m02s") == "📍 phase: 2 execution · 1m02s"


@pytest.mark.parametrize("line", ["unknown-word: left alone", "no prefix here", ""])
def test_unknown_lines_pass_through(line: str) -> None:
    assert style_line(line) == line


def test_every_style_has_one_emoji_and_known_tone() -> None:
    for emoji, tone, rules in STYLES.values():
        assert emoji and " " not in emoji
        assert tone in echo_style.TONE_COLOR
        assert all(rule_tone in echo_style.TONE_COLOR for _, rule_tone in rules)


@pytest.mark.parametrize(
    ("isatty", "env", "expected"),
    [
        (True, {}, True),
        (True, {"TERM": "xterm-256color"}, True),
        (False, {}, False),
        (True, {"NO_COLOR": "1"}, False),
        (True, {"NO_COLOR": ""}, True),
        (True, {"TERM": "dumb"}, False),
    ],
)
def test_should_style(isatty: bool, env: dict[str, str], expected: bool) -> None:
    assert should_style(isatty, env) is expected


def _fake_run(lines: list[str]) -> Callable[..., None]:
    def fake(_project: str, *, echo: Callable[[str], None], **_kwargs: object) -> None:
        for line in lines:
            echo(line)

    return fake


def test_run_output_is_byte_identical_when_piped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("caobunga.cli.execute_run", _fake_run(LINES))
    result = CliRunner().invoke(cli, ["run", "app"])
    assert result.exit_code == 0, result.output
    assert result.output == "".join(f"{line}\n" for line in LINES)


def _tty(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setattr(echo_style.sys.stdout, "isatty", lambda: True, raising=False)
    for key in ("NO_COLOR", "TERM"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_run_echo_styles_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(echo_style.click, "echo", seen.append)
    _tty(monkeypatch, {"TERM": "xterm-256color"})
    echo = run_echo()
    for line in LINES:
        echo(line)
    assert seen == [style_line(line) for line in LINES]
    assert seen[0].startswith("🚀 \x1b[")
    assert seen[-2:] == LINES[-2:]


def test_run_echo_is_plain_under_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch, {"TERM": "xterm-256color", "NO_COLOR": "1"})
    assert run_echo() is click.echo


def test_run_styles_echo_and_header_but_not_the_dump_on_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cli.run` routes loop echoes and the `run:` header through the seam."""
    record = object()

    def fake(_project: str, *, echo: Callable[[str], None], on_run: Callable[..., None]) -> None:
        for line in LINES:
            echo(line)
        on_run(record)

    monkeypatch.setattr(echo_style, "should_style", lambda *_args: True)
    monkeypatch.setattr("caobunga.cli.execute_run", fake)
    monkeypatch.setattr("caobunga.cli.dump_run_record", lambda _r: "outcome: closed\n")
    result = CliRunner().invoke(cli, ["run", "app"], color=True)
    assert result.exit_code == 0, result.output
    expected = [*(style_line(line) for line in LINES), style_line("run:"), "outcome: closed"]
    assert result.output.splitlines() == expected
    assert expected[-2].startswith("📄 run:")


#: Verdict tokens the rules match, by the enum that owns them. A renamed value
#: would silently fall back to the prefix default; this pins them.
RULE_TOKENS = {
    StopReason: {"complete", "incomplete", "outcome"},
    InvocationStatus: {"complete", "incomplete", "empty", "running"},
    SuiteVerdict: {"green", "red", "timeout", "refused"},
}


@pytest.mark.parametrize("enum", list(RULE_TOKENS))
def test_rule_tokens_are_live_enum_values(enum: type[StrEnum]) -> None:
    assert RULE_TOKENS[enum] <= {member.value for member in enum}


RUN_PATH_MODULES = "loop launch wait finish report invocation run_echo suite suite_verbs".split()
_ECHO_PREFIX = re.compile(r'(?:echo|say)\(\s*f?"([a-z][a-z-]*):')


def test_every_run_path_echo_prefix_is_styled() -> None:
    """A new `<word>:` echo in the run path must get a `STYLES` row."""
    root = Path(echo_style.__file__).parent
    emitted = {
        prefix
        for module in RUN_PATH_MODULES
        for prefix in _ECHO_PREFIX.findall((root / f"{module}.py").read_text())
    }
    assert {"launch", "progress", "drain", "invocation"} <= emitted
    assert emitted - set(STYLES) == set()
