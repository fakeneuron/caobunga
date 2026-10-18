import { useQuery } from '@tanstack/react-query'

import { fetchProjects } from '../api/client'
import ErrorState from '../components/ErrorState'
import FleetTable from '../components/FleetTable'
import LoadingState from '../components/LoadingState'

export default function FleetPage() {
  const { data, error, isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: fetchProjects,
  })

  if (isLoading) return <LoadingState label="Loading fleet…" />
  if (error) {
    const message =
      error instanceof Error ? error.message : 'Failed to load projects.'
    return <ErrorState message={message} />
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Fleet</h1>
        <p className="mt-1 text-sm text-slate-500">
          Registered projects with current task and last closure.
        </p>
      </div>
      <FleetTable projects={data?.projects ?? []} />
    </div>
  )
}
