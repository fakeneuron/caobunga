export default function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <p className="text-sm text-slate-500" role="status" aria-live="polite">
      {label}
    </p>
  )
}
