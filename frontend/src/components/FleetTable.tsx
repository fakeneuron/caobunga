import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { Link } from 'react-router-dom'

import type { ProjectListItem } from '../api/types'
import { Dash, EnabledBadge, OptionalText, TierBadge } from './display'

const columnHelper = createColumnHelper<ProjectListItem>()

const columns = [
  columnHelper.accessor('id', {
    header: 'Project',
    cell: (info) => (
      <Link
        to={`/projects/${info.getValue()}`}
        className="font-medium text-sky-700 hover:text-sky-900 hover:underline"
      >
        {info.getValue()}
      </Link>
    ),
  }),
  columnHelper.accessor('tier', {
    header: 'Tier',
    cell: (info) => <TierBadge tier={info.getValue()} />,
  }),
  columnHelper.accessor('enabled', {
    header: 'Status',
    cell: (info) => <EnabledBadge enabled={info.getValue()} />,
  }),
  columnHelper.accessor('coverage', {
    header: 'Coverage',
    cell: (info) => {
      // `marked/open`, not a percentage: the operator seeds a countable number
      // of PLAN lines, and a ratio hides how much work that is.
      const coverage = info.getValue()
      if (!coverage) return <Dash />
      // Unparsed rows (CBN-115) sit beside the tally, never inside `open` —
      // the affix warns that the figure is missing dropped rows.
      const unparsedTitle = coverage.unparsed
        ? `, ${coverage.unparsed} unparsed row${coverage.unparsed === 1 ? '' : 's'} dropped by the grammar`
        : ''
      return (
        <span
          className="font-mono text-xs"
          title={`${coverage.marked} marked, ${coverage.denied} denied of ${coverage.open} open${unparsedTitle}`}
        >
          {coverage.marked}/{coverage.open}
          {coverage.unparsed > 0 ? (
            <span className="text-amber-600"> ⚠{coverage.unparsed}</span>
          ) : null}
        </span>
      )
    },
  }),
  columnHelper.accessor('tree', {
    header: 'Tree',
    cell: (info) => {
      // The gate's verdict, beside coverage rather than after a refusal
      // (CBN-130): a repo can read fully covered and still refuse at exit 2
      // because someone is standing in its working tree.
      const tree = info.getValue()
      if (!tree) return <Dash />
      if (!tree.readable) {
        return (
          <span className="font-mono text-xs text-amber-600" title="git could not report">
            unreadable
          </span>
        )
      }
      if (tree.clean) {
        return <span className="font-mono text-xs text-slate-500">clean</span>
      }
      return (
        <span
          className="font-mono text-xs text-amber-600"
          title={`${tree.blocking} uncommitted change${tree.blocking === 1 ? '' : 's'} would refuse the next launch`}
        >
          dirty ({tree.blocking})
        </span>
      )
    },
  }),
  columnHelper.accessor('current', {
    header: 'Current',
    cell: (info) => {
      const current = info.getValue()
      if (!current) return <OptionalText value={null} />
      const label = current.shortname
        ? `${current.task_id} (${current.shortname})`
        : current.task_id
      return <span className="font-mono text-xs">{label}</span>
    },
  }),
  columnHelper.accessor('last_run', {
    header: 'Last task',
    cell: (info) => {
      const last = info.getValue()
      return <OptionalText value={last?.task_id ?? null} />
    },
  }),
  columnHelper.accessor('last_run', {
    id: 'last_outcome',
    header: 'Outcome',
    cell: (info) => {
      const last = info.getValue()
      return <OptionalText value={last?.outcome ?? null} />
    },
  }),
  columnHelper.accessor('last_run', {
    id: 'last_closed',
    header: 'Closed',
    cell: (info) => {
      const last = info.getValue()
      return <OptionalText value={last?.closed_at ?? null} />
    },
  }),
  columnHelper.accessor('last_run', {
    id: 'last_summary',
    header: 'Summary',
    cell: (info) => {
      const last = info.getValue()
      if (!last?.summary) return <OptionalText value={null} />
      return (
        <span className="line-clamp-2 max-w-xs text-sm text-slate-600" title={last.summary}>
          {last.summary}
        </span>
      )
    },
  }),
]

type FleetTableProps = {
  projects: ProjectListItem[]
}

export default function FleetTable({ projects }: FleetTableProps) {
  // TanStack Table's useReactTable() returns fresh functions every render by
  // design; the React Compiler can't memoize across that boundary, so this
  // component stays unmemoized here. Accepted, not a bug.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data: projects,
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  if (projects.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-slate-200 bg-white px-4 py-8 text-center text-sm text-slate-500">
        No registered projects. Use <code className="font-mono">caobunga register</code> to add
        one.
      </p>
    )
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
      <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
        <thead className="bg-slate-50">
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <th
                  key={header.id}
                  scope="col"
                  className="px-4 py-3 text-xs font-semibold uppercase tracking-wide text-slate-500"
                >
                  {header.isPlaceholder
                    ? null
                    : flexRender(header.column.columnDef.header, header.getContext())}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody className="divide-y divide-slate-100">
          {table.getRowModel().rows.map((row) => (
            <tr key={row.id} className="hover:bg-slate-50/80">
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id} className="px-4 py-3 align-top">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
