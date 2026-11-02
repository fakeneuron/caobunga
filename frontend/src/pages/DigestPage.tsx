import { useQuery } from '@tanstack/react-query'

import { fetchDigest } from '../api/client'
import ErrorState from '../components/ErrorState'
import LoadingState from '../components/LoadingState'
import MarkdownPanel from '../components/MarkdownPanel'

export default function DigestPage() {
  const { data, error, isLoading } = useQuery({
    queryKey: ['digest'],
    queryFn: fetchDigest,
  })

  if (isLoading) return <LoadingState label="Loading digest…" />
  if (error) {
    const message = error instanceof Error ? error.message : 'Failed to load digest.'
    return <ErrorState message={message} />
  }
  if (!data) return null

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Digest</h1>
        <p className="mt-1 text-sm text-slate-500">
          Today&rsquo;s fleet digest, the same body as{' '}
          <code className="font-mono">caobunga digest</code>.
        </p>
      </div>
      <MarkdownPanel title="Daily fleet digest" body={data.markdown} />
    </div>
  )
}
