export default function MarkdownPanel({
  title,
  body,
}: {
  title: string
  body: string | null
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white">
      <h2 className="border-b border-slate-100 px-4 py-3 text-sm font-semibold text-slate-800">
        {title}
      </h2>
      {body ? (
        <pre className="max-h-96 overflow-auto whitespace-pre-wrap px-4 py-3 font-mono text-xs leading-relaxed text-slate-700">
          {body}
        </pre>
      ) : (
        <p className="px-4 py-3 text-sm text-slate-400">No report yet.</p>
      )}
    </section>
  )
}
