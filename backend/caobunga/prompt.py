"""The worker prompt: dispatch and continue shapes, and the posture cues they carry.

`caobunga.prompt` owns what a fresh or stranded worker is told to do — the
`--unattended` / `--fast` posture sentence, the pinned-skill cue, the
foreground-only fact, the park-check cue, and the follow-up-filing cue —
composed by `worker_message` into the four prompt shapes `docs/CONTRACT.md`
§"Run loop" pins byte-for-byte. A leaf module: only `NextTask` (for the type hint) and
`Path` cross in, and nothing calls back into `loop.py`. Split out of
`loop.py` at CBN-EPIC-232.3.
"""

from __future__ import annotations

from pathlib import Path

from caobunga.flaitron import NextTask

# The posture sentence appended to the `in-progress` continue prompt (CBN-109).
# That prompt is prose, not a `/ft-task` invocation, so there is no token to hang
# the flag on — but the flag was never the point, the posture was, and prose
# carries a posture fine. The unattended variant states the park action inline
# and then *points* at the target's own fragment rather than restating flaitron's
# recipe: `supports_unattended` already proved that file is on disk whenever this
# variant is sent, and a caobunga-side paraphrase would make a flaitron release a
# caobunga contract change (CONTRACT §"Fleet boundary"). `{module}` is the path
# that probe resolved (`unattended_module_rel`), not a literal: a core-less
# target keeps the file at `claude/skills/ft-task/`, and the prompt used to name
# the submodule path there, pointing at nothing (CBN-189).
CONTINUE_UNATTENDED = (
    "Run under the --unattended posture (`{module}`): at any gate "
    "you cannot answer, park the tasknote — `status: blocked` plus "
    "`park-reason: <code> — <prose>` — and stop."
)
# The degraded twin. A pin that does not recognize `--unattended` ships no
# `unattended-mode.md` either, so pointing at one would point at nothing; name
# the posture that pin does honor. Same CBN-108 stance as the fresh-launch
# degrade — never worse than the baseline CBN-EPIC-14.3 replaced.
CONTINUE_FAST = (
    "Run under the --fast posture (this pin does not recognize --unattended): "
    "suppress the visual-confirmation ask and commit autonomously at closure."
)

# The pinned-skill cue (CBN-291), spliced into both fresh-launch shapes after the
# posture flag. Claude Code resolves `/ft-task` from the personal
# `~/.claude/skills/` ahead of the repo's `.claude/skills/` wire, so a target
# pinned behind the operator's global flaitron checkout launches a skill whose
# fragments its pin does not ship. A fleet repo's TEST-004 (20261007T150557122Z)
# declined on a missing `preamble.md`; the rerun three minutes later fell back to
# the pinned SKILL.md on its own and closed. This sentence makes that fallback an
# instruction instead of a coin flip. It keys on *which copy* loaded, not on a
# missing fragment, so skew whose fragments all resolve is covered too.
# `{skill}` is `layout.pinned_skill_rel` — the probed skill dir's SKILL.md, and
# only when it is on disk; with none the cue is omitted rather than naming a
# missing file. Continue shapes omit it: they never invoke `/ft-task`.
PINNED_SKILL = (
    "This repo pins /ft-task at `{skill}`: if the copy you loaded is a different "
    "file, follow the pinned one instead."
)

# The foreground-only fact (CBN-199), appended to every prompt shape ahead of
# the follow-up cue. Three observed `worker-exited` failures (CBN-167, CBN-196,
# a fleet repo's CORE-11.3 — all Sonnet under `claude -p`) end the same way: the worker
# runs its Phase 3 suite with `run_in_background`, then ends its turn to "wait
# for the notification" — a headless one-shot session has no next turn, so the
# process exits 0 with the work done, uncommitted, and the tasknote open.
# Claude Code exposes no switch for this (only `CLAUDE_AUTO_BACKGROUND_TASKS`,
# which forces the opposite), so state the harness fact in the prompt instead:
# this is the launcher's fact to state, not flaitron's — the posture module
# cannot know how the harness was invoked. Provider-neutral on purpose: any
# CLI that offers a background-task primitive can strand a headless one-shot
# session the same way.
FOREGROUND_ONLY = (
    "This session is headless and one-shot: run every command in the "
    "foreground and wait for it to finish — a background task's completion "
    "never re-invokes you, and ending your turn ends the session."
)

# The park-check cue (CBN-327), appended to every prompt shape after the
# foreground-only fact. CBN-274's second Codex retry (`20261009T152253989Z`,
# a fleet repo's CORE-243) wrote `park-reason: input-needed` but left `status:
# in-progress`, so the run correctly recorded failed / worker-exited and the
# drain stopped. Close detection reads `status:` alone (CBN-186: a malformed
# reason costs the reason, never the park), and that stays — this asks, it
# does not repair. Only the continue shape named the park shape; the fresh
# launches leaned on the pinned skill. Static, with no degrade: it asks for
# `status: blocked` only, the one field every pin's park writes, so a pin
# without unattended parking is asked for nothing it cannot produce.
PARK_CHECK = (
    "Before you end the session, re-read your tasknote: if you parked it, "
    "confirm its frontmatter says `status: blocked` — a `park-reason:` line "
    "without that status does not park the task."
)

# The follow-up cue (CBN-138.2), appended to every prompt shape. CBN-121 built
# the *read* side and deliberately left this prompt alone, on the ground that
# asking a worker for a heading would be caobunga defining a producer's format.
# That was too broad in three ways, and CONTRACT §"Follow-up capture" now records
# the narrower rule: the cue names a *flaitron skill*, inventing no format at
# all; `worker_message` already asks a producer for an on-disk shape earlier in
# the same prompt (`status: blocked`, in the continue posture and in
# `PARK_CHECK`), so asking cannot itself be the violation; and
# `close._FOLLOWUP_HEADING` stays the tolerant prefix family, so a worker that
# ignores the cue is read exactly as before. A request narrows no read — which
# is the whole distinction. Unprobed since the flaitron v7 hard cut (CBN-330):
# every target pins v7, whose `/ft-file-task` accepts `--unattended`.
FOLLOWUP_FILE = (
    "If you discover work beyond this task's scope, file it before closing: "
    "`/ft-file-task --unattended`."
)


def worker_message(
    candidate: NextTask,
    *,
    unattended: bool,
    unattended_module: Path,
    pinned_skill: Path | None,
) -> str:
    """Prompt for a fresh worker, or the continue prompt for a stranded note.

    `unattended` is the target's probed capability, not a preference — a pin
    that does not recognize `--unattended` routes it into an AskUserQuestion
    nobody is there to answer, which is the banner the flag exists to prevent
    (CBN-108). Degrade to `--fast`: every pin accepts it, and it is the prompt
    CBN-EPIC-14.3 replaced, so the fallback is never worse than that baseline.

    Both branches carry the posture. The continue branch reached it late
    (CBN-109): CBN-EPIC-14.3 read the missing flag as a syntactic problem and
    left the prose bare, and CBN-EPIC-14.5 then hit that branch live — it is
    reached only after a prior session died mid-task, so it is the one dispatch
    path where things have already gone wrong.

    `unattended_module` is the repo-relative `unattended-mode.md` path the
    continue prompt names, resolved by `unattended_module_rel` from the same
    skill dir the probe read (CBN-189). Only the unattended continue shape
    renders it; the other three ignore it.

    `pinned_skill` is `pinned_skill_rel`'s answer (CBN-291): both fresh-launch
    shapes name it, and omit the cue when it is None (no pinned SKILL.md on
    disk). The continue shapes ignore it.

    Required keywords on purpose, all of them. A defaulted parameter would keep
    every existing call site compiling and reinstate exactly the unconditional
    send each guard exists to prevent — or, for the module path, the pinned
    literal that was wrong on a core-less target.
    """
    posture = (
        CONTINUE_UNATTENDED.format(module=unattended_module.as_posix())
        if unattended
        else CONTINUE_FAST
    )
    note = candidate.tasknote
    if note is not None and note.status == "in-progress":
        return (
            f"Continue {candidate.task_id}. Drive the remaining flaitron phases "
            f"in `{note.path}` through closure. "
            "Do not re-invoke /ft-task (that skill is start-only). "
            f"{posture} {FOREGROUND_ONLY} {PARK_CHECK} {FOLLOWUP_FILE}"
        )
    flag = "--unattended" if unattended else "--fast"
    pinned = (
        "" if pinned_skill is None else PINNED_SKILL.format(skill=pinned_skill.as_posix()) + " "
    )
    return (
        f"/ft-task {candidate.task_id} {flag} {pinned}"
        f"{FOREGROUND_ONLY} {PARK_CHECK} {FOLLOWUP_FILE}"
    )
