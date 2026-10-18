/**
 * Seconds as a compact span, shared by the run-history rows and the fleet
 * stats tables.
 *
 * Second precision below the hour is deliberate (CBN-EPIC-12.6 F5): worker runs
 * land squarely in the 1–60m band, and rounding to whole minutes dropped up to
 * 30s — enough that fleet medians would not reconcile with the per-run rows they
 * aggregate. Above an hour, minutes suffice.
 *
 * This lives outside `components/` because it is a pure function with two
 * component callers; `display.tsx` exporting it mixed the module's export
 * surface and tripped eslint `react-refresh/only-export-components`, the same
 * rule CBN-EPIC-12.4 resolved by shrinking the surface rather than silencing it.
 */
export function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) {
    const rest = seconds % 60
    return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`
  }
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}
