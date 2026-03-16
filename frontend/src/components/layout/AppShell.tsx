import { useState, useEffect, useRef, useCallback } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuthStore } from '../../stores/authStore'
import { projects as projectsApi } from '../../services/api'
import type { Project } from '../../types'

export default function AppShell({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuthStore()
  const location = useLocation()
  const navigate = useNavigate()
  const [projectList, setProjectList] = useState<Project[]>([])
  const [expanded, setExpanded] = useState(false)
  const [pinned, setPinned] = useState(true) // locked open by default
  const [mobileOpen, setMobileOpen] = useState(false)
  const collapseTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const isOpen = expanded || pinned

  const handleMouseEnter = useCallback(() => {
    if (collapseTimer.current) {
      clearTimeout(collapseTimer.current)
      collapseTimer.current = null
    }
    if (!pinned) setExpanded(true)
  }, [pinned])

  const handleMouseLeave = useCallback(() => {
    if (!pinned) {
      collapseTimer.current = setTimeout(() => setExpanded(false), 250)
    }
  }, [pinned])

  // Close mobile sidebar on navigation
  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  useEffect(() => {
    projectsApi.list().then((p) => setProjectList(p as Project[])).catch(() => {})
  }, [location.pathname])

  const linkCls = (active: boolean) =>
    `flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors whitespace-nowrap ${
      active
        ? 'bg-gray-900 text-white'
        : 'text-gray-500 hover:text-gray-300 hover:bg-gray-900/50'
    }`

  // Text label opacity — fades in/out with sidebar (desktop only)
  const labelCls = `transition-opacity duration-200 ${isOpen ? 'opacity-100' : 'opacity-0'}`

  const sidebarContent = (isMobile: boolean) => (
    <div className="w-60 h-full flex flex-col">
      {/* Header */}
      <div className="h-[53px] flex items-center gap-3 px-4 border-b border-gray-900">
        <div className="w-5 h-5 rounded bg-indigo-500/20 flex items-center justify-center shrink-0">
          <span className="text-[10px] font-bold text-indigo-400">A</span>
        </div>
        <span className={`text-sm font-semibold text-white tracking-wide whitespace-nowrap ${isMobile ? '' : labelCls}`}>
          Agent Platform
        </span>
        {/* Pin/unpin button — desktop only */}
        {!isMobile && (
          <button
            onClick={() => setPinned(!pinned)}
            className={`ml-auto shrink-0 transition-opacity duration-200 ${
              isOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'
            } ${
              pinned
                ? 'text-indigo-400 hover:text-indigo-300'
                : 'text-gray-600 hover:text-gray-400'
            }`}
            title={pinned ? 'Unlock sidebar (collapse on hover-out)' : 'Lock sidebar open'}
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              {pinned ? (
                <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 10.5V6.75a4.5 4.5 0 119 0v3.75M3.75 21.75h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H3.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
              )}
            </svg>
          </button>
        )}
        {/* Close button — mobile only */}
        {isMobile && (
          <button
            onClick={() => setMobileOpen(false)}
            className="ml-auto text-gray-500 hover:text-gray-300 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-2 py-3 space-y-0.5 overflow-y-auto">
        {/* Dashboard */}
        <Link to="/" className={linkCls(location.pathname === '/')} title="Dashboard">
          <svg className="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" />
          </svg>
          <span className={isMobile ? '' : labelCls}>Dashboard</span>
        </Link>

        {/* Projects section header */}
        <div className="pt-5 pb-1.5 px-3 flex items-center justify-between">
          <span className={`text-[11px] font-medium text-gray-600 uppercase tracking-widest whitespace-nowrap ${isMobile ? '' : labelCls}`}>
            Projects
          </span>
          <button
            onClick={() => navigate('/projects/new')}
            className="text-gray-600 hover:text-indigo-400 transition-colors shrink-0"
            title="New project"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
          </button>
        </div>

        {/* Project list */}
        {projectList.map((p) => {
          const isActive = location.pathname.startsWith(`/projects/${p.id}`)
          const isChatActive = location.pathname === `/projects/${p.id}/chat`
          return (
            <div key={p.id} className="group flex items-center">
              <Link
                to={`/projects/${p.id}`}
                className={`flex-1 flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors whitespace-nowrap ${
                  isActive
                    ? 'bg-gray-900 text-white'
                    : 'text-gray-500 hover:text-gray-300 hover:bg-gray-900/50'
                }`}
                title={p.name}
              >
                {p.icon_url ? (
                  <img
                    src={p.icon_url}
                    alt=""
                    className="w-5 h-5 shrink-0 object-contain"
                    onError={(e) => {
                      (e.target as HTMLImageElement).style.display = 'none'
                      ;(e.target as HTMLImageElement).nextElementSibling?.classList.remove('hidden')
                    }}
                  />
                ) : null}
                <span
                  className={`w-2 h-2 rounded-full shrink-0 ${isActive ? 'bg-indigo-500' : 'bg-gray-700'} ${
                    p.icon_url ? 'hidden' : ''
                  }`}
                />
                <span className={`truncate ${isMobile ? '' : labelCls}`}>{p.name}</span>
              </Link>
              <Link
                to={`/projects/${p.id}/chat`}
                className={`${isMobile ? 'flex' : 'hidden group-hover:flex'} items-center px-1.5 transition-colors ${
                  isChatActive ? 'text-indigo-400' : 'text-gray-600 hover:text-gray-400'
                }`}
                title="Chat with AI"
              >
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
                </svg>
              </Link>
              <Link
                to={`/projects/${p.id}/settings`}
                className={`${isMobile ? 'flex' : 'hidden group-hover:flex'} items-center px-1.5 text-gray-600 hover:text-gray-400 transition-colors`}
                title="Project settings"
              >
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.431.992a6.759 6.759 0 010 .255c-.007.378.138.75.43.99l1.005.828c.424.35.534.954.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
              </Link>
            </div>
          )
        })}

        {/* Bottom nav links */}
        <div className="pt-5 space-y-0.5">
          <Link to="/agents" className={linkCls(location.pathname === '/agents')} title="Agents">
            <svg className="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M18 18.72a9.094 9.094 0 003.741-.479 3 3 0 00-4.682-2.72m.94 3.198l.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0112 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 016 18.719m12 0a5.971 5.971 0 00-.941-3.197m0 0A5.995 5.995 0 0012 12.75a5.995 5.995 0 00-5.058 2.772m0 0a3 3 0 00-4.681 2.72 8.986 8.986 0 003.74.477m.94-3.197a5.971 5.971 0 00-.94 3.197M15 6.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z" />
            </svg>
            <span className={isMobile ? '' : labelCls}>Agents</span>
          </Link>
          <Link to="/settings" className={linkCls(location.pathname === '/settings')} title="Settings">
            <svg className="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M10.343 3.94c.09-.542.56-.94 1.11-.94h1.093c.55 0 1.02.398 1.11.94l.149.894c.07.424.384.764.78.93.398.164.855.142 1.205-.108l.737-.527a1.125 1.125 0 011.45.12l.773.774c.39.389.44 1.002.12 1.45l-.527.737c-.25.35-.272.806-.107 1.204.165.397.505.71.93.78l.893.15c.543.09.94.56.94 1.109v1.094c0 .55-.397 1.02-.94 1.11l-.893.149c-.425.07-.765.383-.93.78-.165.398-.143.854.107 1.204l.527.738c.32.447.269 1.06-.12 1.45l-.774.773a1.125 1.125 0 01-1.449.12l-.738-.527c-.35-.25-.806-.272-1.204-.107-.397.165-.71.505-.78.929l-.15.894c-.09.542-.56.94-1.11.94h-1.094c-.55 0-1.019-.398-1.11-.94l-.148-.894c-.071-.424-.384-.764-.781-.93-.398-.164-.854-.142-1.204.108l-.738.527c-.447.32-1.06.269-1.45-.12l-.773-.774a1.125 1.125 0 01-.12-1.45l.527-.737c.25-.35.273-.806.108-1.204-.165-.397-.506-.71-.93-.78l-.894-.15c-.542-.09-.94-.56-.94-1.109v-1.094c0-.55.398-1.02.94-1.11l.894-.149c.424-.07.765-.383.93-.78.165-.398.143-.854-.108-1.204l-.526-.738a1.125 1.125 0 01.12-1.45l.773-.773a1.125 1.125 0 011.45-.12l.737.527c.35.25.807.272 1.204.107.397-.165.71-.505.78-.929l.15-.894z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
            <span className={isMobile ? '' : labelCls}>Settings</span>
          </Link>
        </div>
      </nav>

      {/* User footer */}
      <div className="px-2 py-3 border-t border-gray-900">
        <div className="flex items-center gap-3 px-3 whitespace-nowrap">
          <div className="w-6 h-6 rounded-full bg-gray-800 flex items-center justify-center text-[10px] font-medium text-gray-400 shrink-0">
            {user?.display_name?.charAt(0).toUpperCase()}
          </div>
          <span className={`text-sm text-gray-500 truncate flex-1 ${isMobile ? '' : labelCls}`}>{user?.display_name}</span>
          <button
            onClick={logout}
            className={`text-xs text-gray-600 hover:text-gray-400 transition-colors shrink-0 ${isMobile ? '' : labelCls}`}
          >
            Sign out
          </button>
        </div>
      </div>
    </div>
  )

  return (
    <div className="flex h-screen bg-gray-950">
      {/* Mobile sidebar overlay */}
      <div
        className={`fixed inset-0 z-30 bg-black/50 md:hidden transition-opacity ${mobileOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
        onClick={() => setMobileOpen(false)}
      />
      <aside
        className={`fixed inset-y-0 left-0 z-40 w-60 bg-gray-950 border-r border-gray-900 md:hidden transform transition-transform duration-300 ease-[cubic-bezier(0.4,0,0.2,1)] ${
          mobileOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        {sidebarContent(true)}
      </aside>

      {/* Desktop sidebar — pinned (locked open) by default. Users can unpin to get hover-expand behavior. */}
      <aside
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
        className={`hidden md:block ${
          isOpen ? 'w-60' : 'w-14'
        } bg-gray-950 border-r border-gray-900 transition-[width] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)] overflow-hidden shrink-0`}
      >
        {sidebarContent(false)}
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto bg-gray-950">
        {/* Mobile hamburger button */}
        <button
          onClick={() => setMobileOpen(true)}
          className="md:hidden fixed top-3 left-3 z-20 p-2 bg-gray-900 border border-gray-800 rounded-lg text-gray-400 hover:text-white transition-colors"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
          </svg>
        </button>
        {children}
      </main>
    </div>
  )
}
