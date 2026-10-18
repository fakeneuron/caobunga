import { Link, NavLink } from 'react-router-dom'

/** Navigation only — the dashboard is read-only in v1, so no action ever lands here. */
const NAV = [
  { to: '/', label: 'Fleet' },
  { to: '/stats', label: 'Stats' },
]

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-4">
          <div className="flex items-baseline gap-6">
            <Link to="/" className="text-lg font-semibold tracking-tight text-slate-900">
              Caobunga
            </Link>
            <nav className="flex gap-4">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end
                  className={({ isActive }) =>
                    isActive
                      ? 'text-sm font-medium text-sky-700 underline underline-offset-4'
                      : 'text-sm text-slate-500 hover:text-slate-800'
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>
          <p className="text-sm text-slate-500">Status dashboard — read only</p>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8">{children}</main>
    </div>
  )
}
