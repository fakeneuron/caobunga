import type { NewRow, RunCounts, RunRecord } from '../api/types'
import { OUTCOMES } from '../api/types'
import { formatSeconds } from '../lib/duration'
import { MetaRow, OptionalText, OutcomeBadge } from './display'

/**
 * Duration is `closed_at - started_at`, derived here rather than stored —
 * see docs/CONTRACT.md §"Run record". Absent when the record predates
 * CBN-EPIC-12.2 and carries no start clock. The seconds→string half lives in
 * `lib/duration.ts` because the fleet stats tables format raw seconds too.
 */
function formatDuration(run: RunRecord): string | null {
  if (!run.started_at) return null
  const ms = Date.parse(run.closed_at) - Date.parse(run.started_at)
  if (!Number.isFinite(ms) || ms < 0) return null
  return formatSeconds(Math.round(ms / 1000))
}

/**
 * Split runs into invocation groups, preserving the served newest-first order.
 *
 * The API sorts by `closed_at`, so first-seen key order is already
 * newest-invocation-first — no second sort here, matching the
 * one-explicit-ordering-per-surface habit the API side keeps. Records with no
 * key bucket under `null` and are emitted last rather than dropped, so the
 * rendered rows still sum to `runs.length` (CBN-EPIC-12.7 left every record
 * written before it without a key; today that is all of them on a live store).
 */
function byInvocation(runs: RunRecord[]): { id: string | null; runs: RunRecord[] }[] {
  const groups = new Map<string | null, RunRecord[]>()
  for (const run of runs) {
    const id = run.invocation_id ?? null
    const bucket = groups.get(id)
    if (bucket) bucket.push(run)
    else groups.set(id, [run])
  }
  const rows = [...groups].map(([id, members]) => ({ id, runs: members }))
  return [...rows.filter((row) => row.id !== null), ...rows.filter((row) => row.id === null)]
}

/** Per-invocation outcome tally — the same seeded five keys the served counts use. */
function tally(runs: RunRecord[]): RunCounts {
  const counts = Object.fromEntries(OUTCOMES.map((outcome) => [outcome, 0])) as RunCounts
  for (const run of runs) counts[run.outcome] += 1
  return counts
}

/** Full run record as a `<dl>` — the Last run panel's view. */
export function RunRecordDetail({ run }: { run: RunRecord | null }) {
  if (!run) {
    return <p className="text-sm text-slate-400">No run record yet.</p>
  }

  const entries: [string, string | null | undefined][] = [
    ['Task', run.task_id],
    ['Outcome', run.outcome],
    ['Closed', run.closed_at],
    ['Started', run.started_at ?? null],
    ['Duration', formatDuration(run)],
    ['Escalation', run.escalation_reason ?? null],
    ['Park reason', run.park_reason ?? null],
    ['Hermes review', run.hermes_review ?? null],
    ['Hermes verdict code', run.hermes_verdict_code ?? null],
    ['Hermes verdict', run.hermes_verdict ?? null],
    ['Hermes missing servers', run.hermes_missing_servers?.join(', ') ?? null],
    ['Suite verdict', run.suite?.verdict ?? null],
    ['Summary', run.summary],
    ['Commit', run.commit_sha ?? null],
    ['Provider', run.provider ?? null],
    ['Model', run.model ?? null],
    ['Plan model', run.plan_model ?? null],
    ['Invocation', run.invocation_id ?? null],
    ['Next task', run.next_task ?? null],
    ['Tasknote', run.tasknote_path ?? null],
  ]

  return (
    <dl>
      {entries.map(([label, value]) => (
        <MetaRow key={label} label={label}>
          <OptionalText value={value ?? null} />
        </MetaRow>
      ))}
      {run.follow_ups && (
        <MetaRow label="Follow-ups">
          <FollowUpList items={run.follow_ups} />
        </MetaRow>
      )}
      {run.new_rows && (
        <MetaRow label="New rows">
          <NewRowList rows={run.new_rows} />
        </MetaRow>
      )}
    </dl>
  )
}

/**
 * What this run proposed doing next that nobody filed (CBN-121).
 *
 * Rendered only when the record carries the key. The backend normalizes an empty
 * list to `null` on purpose, so its absence is "proposed none" rather than "not
 * measured" — and an empty row here would say neither. Items arrive already
 * clipped to 500 chars server-side; they wrap rather than truncate, because a
 * follow-up the operator has to hover to read is one they will not act on.
 */
function FollowUpList({ items }: { items: string[] }) {
  return (
    <ul className="list-disc space-y-1 pl-4">
      {items.map((item, index) => (
        <li key={`${index}-${item}`} className="text-slate-700">
          {item}
        </li>
      ))}
    </ul>
  )
}

/**
 * What this run actually wrote into the target's PLAN (CBN-138.3).
 *
 * Follows the follow-up list rather than preceding it, on the digest's ordering
 * and its reason (docs/CONTRACT.md §"Body shape"): a proposal is what the worker
 * said, a new row is what it did — so the proposal prints first and its result
 * second. Each row states its marker state either way; a bare id would leave the
 * reader inferring it from the count, which fails the moment a run files a mix.
 * `caobunga status` spells the fix out; here the chip is the whole signal.
 */
function NewRowList({ rows }: { rows: NewRow[] }) {
  return (
    <ul className="space-y-1">
      {rows.map((row) => (
        <li key={row.task_id} className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-xs text-slate-800">{row.task_id}</span>
          <span
            className={
              row.unattended
                ? 'inline-flex rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700'
                : 'inline-flex rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700'
            }
          >
            {row.unattended ? 'marked' : 'unmarked'}
          </span>
        </li>
      ))}
    </ul>
  )
}

/** Compact history row — the signals an operator needs to judge a run at a glance. */
function RunHistoryRow({ run }: { run: RunRecord }) {
  const duration = formatDuration(run)
  return (
    <li className="border-b border-slate-100 py-2 text-sm last:border-b-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-slate-800">{run.task_id}</span>
        <OutcomeBadge outcome={run.outcome} />
        <span className="text-xs text-slate-500">{run.closed_at}</span>
        {duration && <span className="text-xs text-slate-500">{duration}</span>}
        {run.hermes_review && (
          <span
            className={
              run.hermes_review === 'degraded' ? 'text-xs text-amber-700' : 'text-xs text-slate-400'
            }
          >
            review: {run.hermes_review}
            {run.hermes_verdict_code && ` [${run.hermes_verdict_code}]`}
          </span>
        )}
        {run.suite && (
          <span
            className={
              run.suite.verdict === 'green' ? 'text-xs text-slate-400' : 'text-xs text-rose-700'
            }
          >
            suite: {run.suite.verdict}
          </span>
        )}
        {run.escalation_reason && (
          <span className="font-mono text-xs text-rose-700">{run.escalation_reason}</span>
        )}
        {run.park_reason && (
          <span className="font-mono text-xs text-amber-700">park: {run.park_reason}</span>
        )}
      </div>
      {/* Summaries run to 500 chars; clamp so twelve rows stay scannable. */}
      <p className="mt-1 line-clamp-2 text-slate-600" title={run.summary}>
        {run.summary}
      </p>
    </li>
  )
}

/** One invocation's runs under a header naming the invocation and its own tally. */
function InvocationGroup({ id, runs }: { id: string | null; runs: RunRecord[] }) {
  const counts = tally(runs)
  return (
    <section className="pb-2">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 pb-1 pt-3">
        <span className="font-mono text-xs font-medium text-slate-700">
          {id ?? 'ungrouped (no invocation key)'}
        </span>
        <span className="text-xs text-slate-500">
          {runs.length} {runs.length === 1 ? 'run' : 'runs'}
        </span>
        {OUTCOMES.filter((outcome) => counts[outcome] > 0).map((outcome) => (
          <OutcomeBadge key={outcome} outcome={outcome} count={counts[outcome]} />
        ))}
      </div>
      <ul>
        {runs.map((run) => (
          <RunHistoryRow key={`${run.closed_at}-${run.task_id}`} run={run} />
        ))}
      </ul>
    </section>
  )
}

/**
 * Every closed run for a project, grouped by the invocation that wrote it —
 * newest invocation first, as served. `runs[0]` is the same record the Last run
 * panel shows; repeating it keeps the chip counts and the visible rows in
 * agreement (CBN-EPIC-12.4).
 *
 * Grouping is what makes the page answer "how did *that run* go?" rather than
 * only "how did task X go?" — CBN-EPIC-12.6 F3, where judging one drain meant
 * doing timestamp arithmetic across a flat list. The section headers carry a
 * per-invocation tally; the fixed five-chip row above stays project-wide.
 */
export function RunHistory({ runs, counts }: { runs: RunRecord[]; counts: RunCounts }) {
  return (
    <>
      <div className="flex flex-wrap gap-2 pb-2">
        {OUTCOMES.map((outcome) => (
          <OutcomeBadge key={outcome} outcome={outcome} count={counts[outcome] ?? 0} />
        ))}
      </div>
      {runs.length === 0 ? (
        <p className="pb-2 text-sm text-slate-400">No runs recorded yet.</p>
      ) : (
        byInvocation(runs).map((group) => (
          <InvocationGroup key={group.id ?? 'ungrouped'} id={group.id} runs={group.runs} />
        ))
      )}
    </>
  )
}
