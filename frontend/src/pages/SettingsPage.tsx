import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useAuthStore } from '../stores/authStore'
import ProvidersTab from '../components/settings/ProvidersTab'
import GitProvidersTab from '../components/settings/GitProvidersTab'
import SkillsTab from '../components/settings/SkillsTab'
import McpServersTab from '../components/settings/McpServersTab'
import NotificationsTab from '../components/settings/NotificationsTab'
import EmailConfigTab from '../components/settings/EmailConfigTab'
import UsersTab from '../components/settings/UsersTab'

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

  const tabs: { key: SettingsTab; label: string; adminOnly?: boolean }[] = [
    { key: 'providers', label: 'AI Providers' },
    { key: 'git', label: 'Git Providers' },
    { key: 'skills', label: 'Skills' },
    { key: 'mcp', label: 'MCP Servers' },
    { key: 'notifications', label: 'Notifications' },
    ...(isAdmin ? [
      { key: 'email_config' as const, label: 'Email Config', adminOnly: true },
      { key: 'users' as const, label: 'Users', adminOnly: true },
    ] : []),
  ]

  return (
    <div className="p-4 md:p-6 max-w-3xl mx-auto">
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-white">Settings</h1>
        <p className="text-sm text-gray-500 mt-1">Manage providers, skills, MCP servers, and users.</p>
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 mb-6 border-b border-gray-800 overflow-x-auto">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px shrink-0 whitespace-nowrap ${
              activeTab === tab.key
                ? 'text-indigo-400 border-indigo-400'
                : 'text-gray-500 border-transparent hover:text-gray-300 hover:border-gray-700'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'providers' && <ProvidersTab isAdmin={isAdmin} />}
      {activeTab === 'git' && <GitProvidersTab isAdmin={isAdmin} />}
      {activeTab === 'skills' && <SkillsTab isAdmin={isAdmin} />}
      {activeTab === 'mcp' && <McpServersTab isAdmin={isAdmin} />}
      {activeTab === 'notifications' && <NotificationsTab isAdmin={isAdmin} />}
      {activeTab === 'email_config' && isAdmin && <EmailConfigTab isAdmin={isAdmin} />}
      {activeTab === 'users' && isAdmin && <UsersTab isAdmin={isAdmin} />}
    </div>
  )
}
