import type { FleetStats, RoutingRow, RunCounts, ThroughputDay } from '../api/types'
import { ESCALATIONS, OUTCOMES, ROUTING_DIMENSIONS } from '../api/types'
import { Dash, DurationText, EscalationBadge, OutcomeBadge } from './display'

/** Served as a 3-place fraction; read as a percentage. `null` on an empty fleet. */
function formatRate(rate: number | null): string | null {
  if (rate === null) return null
  return `${Math.round(rate * 1000) / 10}%`
}

/** The fixed five-chip tally, reused for the fleet, each day, and each routing row. */
function OutcomeChips({ counts }: { counts: RunCounts }) {
  return (
    <div className="flex flex-wrap gap-1">
      {OUTCOMES.map((outcome) => (
        <OutcomeBadge key={outcome} outcome={outcome} count={counts[outcome] ?? 0} />
      ))}
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white px-4 py-2">
      <h2 className="py-2 text-sm font-semibold text-slate-800">{title}</h2>
      {children}
    </section>
  )
}

/** Shared table chrome — the fleet table's look without its TanStack machinery. */
function StatsTable({ headers, children }: { headers: string[]; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto pb-2">
      <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
        <thead>
          <tr>
            {headers.map((header) => (
              <th
                key={header}
                scope="col"
                className="px-2 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500"
              >
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
    </div>
  )
}

/** One local calendar day. Days with no runs are absent from the payload, not zero rows. */
function ThroughputTable({ days }: { days: ThroughputDay[] }) {
  if (days.length === 0) {
    return <p className="pb-2 text-sm text-slate-400">No runs recorded yet.</p>
  }
  return (
    <StatsTable headers={['Day', 'Runs', 'Outcomes']}>
      {days.map((day) => (
        <tr key={day.day}>
          <td className="px-2 py-2 align-top font-mono text-xs text-slate-800">{day.day}</td>
          <td className="px-2 py-2 align-top">{day.runs}</td>
          <td className="px-2 py-2 align-top">
            <OutcomeChips counts={day.outcomes} />
          </td>
        </tr>
      ))}
    </StatsTable>
  )
}

/**
 * One routing dimension. A `null` key means the runs had that field unset — the
 * row is kept rather than dropped, which is what makes the rows sum to `runs`
 * for every dimension (docs/CONTRACT.md §"Status API").
 */
function RoutingTable({ rows }: { rows: RoutingRow[] }) {
  if (rows.length === 0) {
    return <p className="pb-2 text-sm text-slate-400">No runs recorded yet.</p>
  }
  return (
    <StatsTable headers={['Key', 'Runs', 'Outcomes', 'Median', 'Worst', 'Counted']}>
      {rows.map((row) => (
        <tr key={row.key ?? 'unset'}>
          <td className="px-2 py-2 align-top font-mono text-xs text-slate-800">
            {row.key ?? <Dash />}
          </td>
          <td className="px-2 py-2 align-top">{row.runs}</td>
          <td className="px-2 py-2 align-top">
            <OutcomeChips counts={row.outcomes} />
          </td>
          <td className="px-2 py-2 align-top text-slate-600">
            <DurationText seconds={row.duration.median_seconds} />
          </td>
          <td className="px-2 py-2 align-top text-slate-600">
            <DurationText seconds={row.duration.max_seconds} />
          </td>
          <td className="px-2 py-2 align-top text-xs text-slate-500">
            {row.duration.counted} of {row.runs}
          </td>
        </tr>
      ))}
    </StatsTable>
  )
}

/**
 * The `GET /api/stats` fold, rendered as chips and tables only — no charting
 * dependency, per the CBN-EPIC-12.7 gate decision this task inherits.
 *
 * Every tally the API seeds renders its full key set including zeros: five
 * outcomes, seven escalations. `null` durations and an empty-fleet
 * `success_rate` render as the dash idiom rather than `0`, matching what the
 * contract says those nulls mean — nothing counted, not nothing happened.
 */
export default function FleetStatsView({ stats }: { stats: FleetStats }) {
  const rate = formatRate(stats.success_rate)
  return (
    <div className="space-y-6">
      <Section title="Fleet">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 pb-3 sm:grid-cols-4">
          {(
            [
              ['Projects', String(stats.projects)],
              ['Runs', String(stats.runs)],
              ['Invocations', String(stats.invocations)],
              ['Closed rate', rate],
            ] as [string, string | null][]
          ).map(([label, value]) => (
            <div key={label}>
              <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
                {label}
              </dt>
              <dd className="text-lg font-semibold text-slate-900">
                {value === null ? <Dash /> : value}
              </dd>
            </div>
          ))}
        </dl>
        <div className="flex flex-wrap gap-2 pb-3">
          {OUTCOMES.map((outcome) => (
            <OutcomeBadge key={outcome} outcome={outcome} count={stats.outcomes[outcome] ?? 0} />
          ))}
        </div>
        <p className="pb-2 text-sm text-slate-600">
          Duration — median <DurationText seconds={stats.duration.median_seconds} /> · worst{' '}
          <DurationText seconds={stats.duration.max_seconds} />{' '}
          <span className="text-xs text-slate-500">
            ({stats.duration.counted} of {stats.runs} counted)
          </span>
        </p>
      </Section>

      <Section title="Escalations">
        <div className="flex flex-wrap gap-2 pb-3">
          {ESCALATIONS.map((reason) => (
            <EscalationBadge key={reason} reason={reason} count={stats.escalations[reason] ?? 0} />
          ))}
        </div>
        {/* The undercount is contract-documented, not a rendering bug — say so here
            rather than leaving an operator to reconcile it against the chips above. */}
        <p className="pb-2 text-xs text-slate-500">
          May total fewer than failed + blocked: records written before CBN-EPIC-12.2 stopped
          without a reason.
        </p>
      </Section>

      <Section title="Throughput">
        <p className="pb-1 text-xs text-slate-500">
          One row per local calendar day that saw runs, newest first.
        </p>
        <ThroughputTable days={stats.throughput} />
      </Section>

      {ROUTING_DIMENSIONS.map((dimension) => (
        <Section key={dimension} title={`Routing — ${dimension.replace('_', ' ')}`}>
          <RoutingTable rows={stats.routing[dimension] ?? []} />
        </Section>
      ))}
    </div>
  )
}
