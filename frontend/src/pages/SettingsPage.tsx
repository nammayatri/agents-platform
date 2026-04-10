import { useSearchParams } from 'react-router-dom'
import { useAuthStore } from '../stores/authStore'
import {
  Settings as SettingsIcon, Cpu, GitBranch, Wand2, Server,
  Bell, Mail, Users, ChevronRight, Blocks, Shield,
} from 'lucide-react'
import ProvidersTab from '../components/settings/ProvidersTab'
import GitProvidersTab from '../components/settings/GitProvidersTab'
import SkillsTab from '../components/settings/SkillsTab'
import McpServersTab from '../components/settings/McpServersTab'
import NotificationsTab from '../components/settings/NotificationsTab'
import EmailConfigTab from '../components/settings/EmailConfigTab'
import UsersTab from '../components/settings/UsersTab'
import type { LucideIcon } from 'lucide-react'

interface SidebarItem {
  key: string
  label: string
  Icon: LucideIcon
  adminOnly?: boolean
}

interface SidebarGroup {
  label: string
  Icon: LucideIcon
  items: SidebarItem[]
}

export default function SettingsPage() {
  const { user } = useAuthStore()
  const isAdmin = user?.role === 'admin'
  const [searchParams, setSearchParams] = useSearchParams()

  const sidebarGroups: SidebarGroup[] = [
    {
      label: 'Integrations',
      Icon: Blocks,
      items: [
        { key: 'providers', label: 'AI Providers', Icon: Cpu },
        { key: 'git', label: 'Git Providers', Icon: GitBranch },
        { key: 'skills', label: 'Skills', Icon: Wand2 },
        { key: 'mcp', label: 'MCP Servers', Icon: Server },
      ],
    },
    {
      label: 'Notifications',
      Icon: Bell,
      items: [
        { key: 'notifications', label: 'Channels', Icon: Bell },
        ...(isAdmin ? [{ key: 'email_config', label: 'Email Config', Icon: Mail, adminOnly: true }] : []),
      ],
    },
    ...(isAdmin ? [{
      label: 'Administration',
      Icon: Shield,
      items: [
        { key: 'users', label: 'Users', Icon: Users, adminOnly: true },
      ],
    }] : []),
  ]

  const activeSection = searchParams.get('section') || 'providers'
  const setActiveSection = (s: string) => setSearchParams({ section: s })

  const activeGroupIdx = sidebarGroups.findIndex(g =>
    g.items.some(i => i.key === activeSection)
  )

  return (
    <div className="p-4 md:p-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center gap-2.5">
          <SettingsIcon className="w-5 h-5 text-gray-500" />
          <h1 className="text-xl font-semibold text-white">Settings</h1>
        </div>
        <p className="text-sm text-gray-500 mt-1">Manage providers, skills, MCP servers, and notifications.</p>
      </div>

      <div className="flex gap-6 min-h-[500px]">
        {/* Sidebar */}
        <nav className="w-48 shrink-0">
          <div className="space-y-1">
            {sidebarGroups.map((group, gi) => {
              const isExpanded = gi === activeGroupIdx
              return (
                <div key={group.label}>
                  <button
                    onClick={() => {
                      if (!isExpanded) setActiveSection(group.items[0].key)
                    }}
                    className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                      isExpanded
                        ? 'text-white bg-gray-900/50'
                        : 'text-gray-500 hover:text-gray-300 hover:bg-gray-900/30'
                    }`}
                  >
                    <group.Icon className="w-3.5 h-3.5" />
                    <span className="flex-1 text-left">{group.label}</span>
                    <ChevronRight className={`w-3 h-3 transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
                  </button>
                  {isExpanded && (
                    <div className="ml-3 mt-0.5 space-y-0.5 border-l border-gray-800 pl-2">
                      {group.items.map((item) => (
                        <button
                          key={item.key}
                          onClick={() => setActiveSection(item.key)}
                          className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded text-xs transition-colors ${
                            activeSection === item.key
                              ? 'text-indigo-400 bg-indigo-500/10'
                              : 'text-gray-500 hover:text-gray-300'
                          }`}
                        >
                          <item.Icon className="w-3 h-3" />
                          {item.label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </nav>

        {/* Content */}
        <div className="flex-1 min-w-0">
          {activeSection === 'providers' && <ProvidersTab isAdmin={isAdmin} />}
          {activeSection === 'git' && <GitProvidersTab isAdmin={isAdmin} />}
          {activeSection === 'skills' && <SkillsTab isAdmin={isAdmin} />}
          {activeSection === 'mcp' && <McpServersTab isAdmin={isAdmin} />}
          {activeSection === 'notifications' && <NotificationsTab isAdmin={isAdmin} />}
          {activeSection === 'email_config' && isAdmin && <EmailConfigTab isAdmin={isAdmin} />}
          {activeSection === 'users' && isAdmin && <UsersTab isAdmin={isAdmin} />}
        </div>
      </div>
    </div>
  )
}
