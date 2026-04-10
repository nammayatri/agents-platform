import { useState, useEffect, useRef, useCallback } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuthStore } from '../../stores/authStore'
import { projects as projectsApi } from '../../services/api'
import { LayoutDashboard, Users, Settings, MessageSquare, Plus, Lock, Unlock, X, Menu } from 'lucide-react'
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

  // Fetch projects once on mount — not on every navigation
  useEffect(() => {
    projectsApi.list().then((p) => setProjectList(p as Project[])).catch(() => {})
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

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
        <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-indigo-500/20 to-indigo-600/10 border border-indigo-500/20 flex items-center justify-center shrink-0">
          <span className="text-xs font-bold text-indigo-400">A</span>
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
            {pinned ? <Lock className="w-3.5 h-3.5" /> : <Unlock className="w-3.5 h-3.5" />}
          </button>
        )}
        {/* Close button — mobile only */}
        {isMobile && (
          <button
            onClick={() => setMobileOpen(false)}
            className="ml-auto text-gray-500 hover:text-gray-300 transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-2 py-3 space-y-0.5 overflow-y-auto">
        {/* Dashboard */}
        <Link to="/" className={linkCls(location.pathname === '/')} title="Dashboard">
          <LayoutDashboard className="w-4 h-4 shrink-0" />
          <span className={isMobile ? '' : labelCls}>Dashboard</span>
        </Link>

        {/* Projects section header */}
        <div className="pt-4 pb-1.5 px-3 flex items-center justify-between border-t border-gray-800/50 mt-3">
          <span className={`text-[11px] font-medium text-gray-600 uppercase tracking-widest whitespace-nowrap ${isMobile ? '' : labelCls}`}>
            Projects
          </span>
          <button
            onClick={() => navigate('/projects/new')}
            className="text-gray-600 hover:text-indigo-400 transition-colors shrink-0"
            title="New project"
          >
            <Plus className="w-3.5 h-3.5" />
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
                    ? 'bg-gray-900 text-white border-l-2 border-indigo-500 rounded-l-none'
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
                <MessageSquare className="w-3.5 h-3.5" />
              </Link>
              <Link
                to={`/projects/${p.id}/settings`}
                className={`${isMobile ? 'flex' : 'hidden group-hover:flex'} items-center px-1.5 text-gray-600 hover:text-gray-400 transition-colors`}
                title="Project settings"
              >
                <Settings className="w-3.5 h-3.5" />
              </Link>
            </div>
          )
        })}

        {/* Bottom nav links */}
        <div className="pt-3 space-y-0.5 border-t border-gray-800/50 mt-3">
          <Link to="/agents" className={linkCls(location.pathname === '/agents')} title="Agents">
            <Users className="w-4 h-4 shrink-0" />
            <span className={isMobile ? '' : labelCls}>Agents</span>
          </Link>
          <Link to="/settings" className={linkCls(location.pathname === '/settings')} title="Settings">
            <Settings className="w-4 h-4 shrink-0" />
            <span className={isMobile ? '' : labelCls}>Settings</span>
          </Link>
        </div>
      </nav>

      {/* User footer */}
      <div className="px-2 py-3 border-t border-gray-900">
        <div className="flex items-center gap-3 px-3 whitespace-nowrap">
          <div className="w-6 h-6 rounded-full bg-indigo-500/20 flex items-center justify-center text-[10px] font-medium text-indigo-400 shrink-0">
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
          <Menu className="w-4 h-4" />
        </button>
        {children}
      </main>
    </div>
  )
}
