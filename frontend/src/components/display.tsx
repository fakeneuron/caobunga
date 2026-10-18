import type { EscalationReason, Outcome } from '../api/types'
import { formatSeconds } from '../lib/duration'

function Dash() {
  return <span className="text-slate-400">—</span>
}

export function OptionalText({ value }: { value: string | null | undefined }) {
  if (!value) return <Dash />
  return <span>{value}</span>
}

export function EnabledBadge({ enabled }: { enabled: boolean }) {
  return (
    <span
      className={
        enabled
          ? 'inline-flex rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700'
          : 'inline-flex rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600'
      }
    >
      {enabled ? 'enabled' : 'disabled'}
    </span>
  )
}

/** Outcome palette. Neutral for the two benign non-`closed` outcomes. */
const OUTCOME_STYLES: Record<Outcome, string> = {
  closed: 'bg-emerald-50 text-emerald-700',
  'de-scoped': 'bg-slate-100 text-slate-600',
  blocked: 'bg-amber-50 text-amber-700',
  failed: 'bg-rose-50 text-rose-700',
  'no-tasknote': 'bg-slate-100 text-slate-600',
}

/**
 * Outcome chip. With `count`, renders as a tally chip for the summary row —
 * a zero count is a real answer, so it renders muted rather than hidden.
 */
export function OutcomeBadge({ outcome, count }: { outcome: Outcome; count?: number }) {
  const muted = count === 0
  return (
    <span
      className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${
        muted ? 'bg-slate-50 text-slate-400' : OUTCOME_STYLES[outcome]
      }`}
    >
      {count === undefined ? outcome : `${outcome} · ${count}`}
    </span>
  )
}

/**
 * Escalation chip. Every escalation reason is a stop, so there is no benign
 * branch here — unlike `OutcomeBadge`, one palette covers all seven. A zero
 * count renders muted rather than hidden: `escalations` seeds all seven keys,
 * so a zero is a real answer (docs/CONTRACT.md §"Status API").
 */
export function EscalationBadge({
  reason,
  count,
}: {
  reason: EscalationReason
  count?: number
}) {
  const muted = count === 0
  return (
    <span
      className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${
        muted ? 'bg-slate-50 text-slate-400' : 'bg-amber-50 text-amber-700'
      }`}
    >
      {count === undefined ? reason : `${reason} · ${count}`}
    </span>
  )
}

/** Optional derived span — `null` (nothing counted) renders as the dash idiom. */
export function DurationText({ seconds }: { seconds: number | null | undefined }) {
  if (seconds === null || seconds === undefined) return <Dash />
  return <span>{formatSeconds(seconds)}</span>
}

export function TierBadge({ tier }: { tier: string }) {
  return (
    <span className="inline-flex rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
      {tier}
    </span>
  )
}

/** Label/value row shared by the detail page's `<dl>` sections. */
export function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-2 border-b border-slate-100 py-2 text-sm last:border-b-0">
      <dt className="font-medium text-slate-500">{label}</dt>
      <dd className="text-slate-800">{children}</dd>
    </div>
  )
}

export { Dash }
