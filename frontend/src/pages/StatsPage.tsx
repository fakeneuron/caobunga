import { useQuery } from '@tanstack/react-query'

import { fetchStats } from '../api/client'
import ErrorState from '../components/ErrorState'
import FleetStatsView from '../components/FleetStats'
import LoadingState from '../components/LoadingState'

export default function StatsPage() {
  const { data, error, isLoading } = useQuery({
    queryKey: ['stats'],
    queryFn: fetchStats,
  })

  if (isLoading) return <LoadingState label="Loading stats…" />
  if (error) {
    const message = error instanceof Error ? error.message : 'Failed to load stats.'
    return <ErrorState message={message} />
  }
  if (!data) return null

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Stats</h1>
        <p className="mt-1 text-sm text-slate-500">
          Every registered project&rsquo;s run records, folded fleet-wide. Unbounded — no window.
        </p>
      </div>
      <FleetStatsView stats={data} />
    </div>
  )
}
