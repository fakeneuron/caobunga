"""Where a target repo's Flaitron artifacts live, and what its pinned skills can do.

The layout vocabulary `caobunga.flaitron` reads producers through: six `*_REL`
paths (five under a target's `.flaitron/` — flaitron v6.0.0's hard-cut rename
of `.flowtron/`, CBN-278 — and one flaitron-self fallback), and the
capability probe that answers what the target's *pinned* `/ft-task` accepts.
A leaf module — `pathlib` only, no sibling import — split out at CBN-143.4.
"""

from __future__ import annotations

from pathlib import Path

from caobunga.text import read_text_or_none

PLAN_REL = Path(".flaitron") / "PLAN.md"
TASKNOTE_DIR_REL = Path(".flaitron") / "tasknote"
# `/ft-file-task --park` stubs (`SPEC/layout.md`). Caobunga never reads these
# — the park stub's content is the operator's, not a producer signal — but the
# path is part of the layout vocabulary this module owns, and `presence.py`
# needs it to tell a worker's filing from an operator's half-edited tree.
SIDEQUEST_DIR_REL = Path(".flaitron") / "sidequest"
# The target's pinned `/ft-task` skill, for the `--unattended` capability probe
# (CBN-108). The submodule path, not the target's `.claude/skills` symlink:
# "pinned" is what the submodule holds, and a target with no skill wired at all
# is a larger failure than this probe answers.
FT_TASK_SKILL_DIR_REL = Path(".flaitron") / "core" / "claude" / "skills" / "ft-task"
# Flaitron-self (no submodule): skills live at the repo's own `claude/skills/`
# tree. Fallback only when `.flaitron/core` is absent (CBN-156) — an adopter
# with a pin keeps the submodule read, even if this tree also exists.
CORE_REL = Path(".flaitron") / "core"
SELF_FT_TASK_SKILL_DIR_REL = Path("claude") / "skills" / "ft-task"
UNATTENDED_MODULE = "unattended-mode.md"
UNATTENDED_TOKEN = "--unattended"  # noqa: S105


def _probe_skill_dir(root: Path, pinned: Path, self_layout: Path) -> Path:
    """Pinned skill dir when `.flaitron/core` exists; else flaitron-self layout.

    Presence of the core path is the whole gate (CBN-156). An empty core still
    counts as present so a broken pin cannot silently read unpinned skills — and
    so does a core whose existence cannot be read (an `OSError`): the
    callers' skip-not-fail answers then come from the reads that follow.

    Probed with `stat()` rather than `Path.exists()`: 3.14's `exists()` answers
    `False` on `EACCES` where 3.13 raises, so only `stat()` tells "absent"
    (`FileNotFoundError` / `NotADirectoryError`) from "unreadable" on both.
    """
    try:
        (root / CORE_REL).stat()
    except (FileNotFoundError, NotADirectoryError):
        return root / self_layout
    except OSError:
        pass
    return root / pinned


def supports_unattended(root: Path) -> bool:
    """Does this target's pinned Flaitron `/ft-task` recognize `--unattended`?

    A capability probe rather than a `**Version:**` floor compare (CBN-108): the
    question that decides the outcome is whether *this* pin's arg parser accepts
    the token, and a tag can be ahead of, behind, or unrelated to the files on
    disk. It is also how CBN-EPIC-14.5 verified the same thing by hand.

    Both halves are required. The module alone would break the skill mid-run;
    the token alone would parse into a missing module.

    When `.flaitron/core` is absent, the same two halves are read from
    `claude/skills/ft-task/` (CBN-156, flaitron-self). A present core never
    falls back.

    **Skip-not-fail**, the policy every other producer read here follows: a
    missing pin *and* missing self-layout, an unreadable directory, or
    undecodable text answers `False` — "assume the flag is not recognized" —
    rather than raising. Callers degrade to `--fast`, which every pin accepts,
    so a wrong `False` costs posture and never a run.
    """
    skill_dir = _probe_skill_dir(root, FT_TASK_SKILL_DIR_REL, SELF_FT_TASK_SKILL_DIR_REL)
    try:
        has_module = (skill_dir / UNATTENDED_MODULE).is_file()
    except OSError:
        return False
    if not has_module:
        return False
    skill = read_text_or_none(skill_dir / "SKILL.md")
    return skill is not None and UNATTENDED_TOKEN in skill


def unattended_module_rel(root: Path) -> Path:
    """Repo-relative path of `unattended-mode.md` a worker in `root` should read.

    Resolved through the same `_probe_skill_dir` gate `supports_unattended`
    uses, so the path the continue prompt names is the file that probe proved
    on disk (CBN-189): the pin's `.flaitron/core/claude/skills/ft-task/` on an
    adopter, `claude/skills/ft-task/` on a core-less flaitron-self (CBN-156).
    Relative because the worker runs in the target repo — the same shape as
    the note path beside it in the prompt. No existence check: the caller only
    sends the unattended variant after `supports_unattended` answered True.
    """
    skill_dir = _probe_skill_dir(root, FT_TASK_SKILL_DIR_REL, SELF_FT_TASK_SKILL_DIR_REL)
    return skill_dir.relative_to(root) / UNATTENDED_MODULE


def pinned_skill_rel(root: Path) -> Path | None:
    """Repo-relative pinned `/ft-task` `SKILL.md`, or None when it is not on disk.

    The fresh-launch prompt names it so a worker that loaded a different copy
    (Claude Code resolves the operator's global skills first) falls back to the
    pin (CBN-291). Same `_probe_skill_dir` gate as `supports_unattended`, but
    with the existence check `unattended_module_rel` skips: this path is sent
    on the `--fast` degrade too, where the probe may have found nothing, and
    the cue must never name a missing file. Skip-not-fail: an unreadable dir
    answers None and the prompt simply omits the cue.
    """
    skill_dir = _probe_skill_dir(root, FT_TASK_SKILL_DIR_REL, SELF_FT_TASK_SKILL_DIR_REL)
    try:
        present = (skill_dir / "SKILL.md").is_file()
    except OSError:
        return None
    return skill_dir.relative_to(root) / "SKILL.md" if present else None
