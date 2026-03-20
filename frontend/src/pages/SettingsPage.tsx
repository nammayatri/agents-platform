import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useAuthStore } from '../stores/authStore'
import { Settings as SettingsIcon, Cpu, GitBranch, Wand2, Server, Bell, Mail, Users } from 'lucide-react'
import ProvidersTab from '../components/settings/ProvidersTab'
import GitProvidersTab from '../components/settings/GitProvidersTab'
import SkillsTab from '../components/settings/SkillsTab'
import McpServersTab from '../components/settings/McpServersTab'
import NotificationsTab from '../components/settings/NotificationsTab'
import EmailConfigTab from '../components/settings/EmailConfigTab'
import UsersTab from '../components/settings/UsersTab'
import { tabBar, tabBtn } from '../styles/classes'
import type { LucideIcon } from 'lucide-react'

type SettingsTab = 'providers' | 'git' | 'skills' | 'mcp' | 'notifications' | 'email_config' | 'users'

export default function SettingsPage() {
  const { user } = useAuthStore()
  const isAdmin = user?.role === 'admin'
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab') as SettingsTab | null
  const validTabs: SettingsTab[] = ['providers', 'git', 'skills', 'mcp', 'notifications', 'email_config', 'users']
  const [activeTab, setActiveTabState] = useState<SettingsTab>(
    tabParam && validTabs.includes(tabParam) ? tabParam : 'providers'
  )
  const setActiveTab = (tab: SettingsTab) => {
    setActiveTabState(tab)
    setSearchParams({ tab })
  }

  const tabs: { key: SettingsTab; label: string; Icon: LucideIcon; adminOnly?: boolean }[] = [
    { key: 'providers', label: 'AI Providers', Icon: Cpu },
    { key: 'git', label: 'Git Providers', Icon: GitBranch },
    { key: 'skills', label: 'Skills', Icon: Wand2 },
    { key: 'mcp', label: 'MCP Servers', Icon: Server },
    { key: 'notifications', label: 'Notifications', Icon: Bell },
    ...(isAdmin ? [
      { key: 'email_config' as const, label: 'Email Config', Icon: Mail, adminOnly: true },
      { key: 'users' as const, label: 'Users', Icon: Users, adminOnly: true },
    ] : []),
  ]

  return (
    <div className="p-4 md:p-6 max-w-3xl mx-auto">
      <div className="mb-6 animate-fade-in">
        <div className="flex items-center gap-2.5">
          <SettingsIcon className="w-5 h-5 text-gray-500" />
          <h1 className="text-xl font-semibold text-white">Settings</h1>
        </div>
        <p className="text-sm text-gray-500 mt-1">Manage providers, skills, MCP servers, and users.</p>
      </div>

      {/* Tab bar */}
      <div className={`${tabBar} mb-6`}>
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={tabBtn(activeTab === tab.key)}
          >
            <tab.Icon className="w-3.5 h-3.5" />
            {tab.label}
          </button>
        ))}
      </div>

      <div className="animate-fade-in">
        {activeTab === 'providers' && <ProvidersTab isAdmin={isAdmin} />}
        {activeTab === 'git' && <GitProvidersTab isAdmin={isAdmin} />}
        {activeTab === 'skills' && <SkillsTab isAdmin={isAdmin} />}
        {activeTab === 'mcp' && <McpServersTab isAdmin={isAdmin} />}
        {activeTab === 'notifications' && <NotificationsTab isAdmin={isAdmin} />}
        {activeTab === 'email_config' && isAdmin && <EmailConfigTab isAdmin={isAdmin} />}
        {activeTab === 'users' && isAdmin && <UsersTab isAdmin={isAdmin} />}
      </div>
    </div>
  )
}
