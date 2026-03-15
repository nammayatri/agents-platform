import { useEffect, useState } from 'react'
import { gitProviders as gitProvidersApi } from '../../services/api'
import type { GitProviderConfig } from '../../types'
import { inputClass, btnPrimary, btnSecondary, btnDanger } from '../../styles/classes'

interface Props {
  isAdmin: boolean
}

const gitProviderDefaults: Record<string, string> = {
  github: 'https://api.github.com',
  gitlab: 'https://gitlab.com',
  bitbucket: 'https://api.bitbucket.org',
}

export default function GitProvidersTab({ isAdmin: _isAdmin }: Props) {
  const [gitProviderList, setGitProviderList] = useState<GitProviderConfig[]>([])
  const [showGitProviderForm, setShowGitProviderForm] = useState(false)
  const [editingGitProviderId, setEditingGitProviderId] = useState<string | null>(null)
  const [gitProviderForm, setGitProviderForm] = useState({
    provider_type: 'github',
    display_name: '',
    api_base_url: '',
    token: '',
  })

  useEffect(() => {
    gitProvidersApi.list().then((g) => setGitProviderList(g as GitProviderConfig[])).catch(() => {})
  }, [])

  const resetGitProviderForm = () => {
    setGitProviderForm({ provider_type: 'github', display_name: '', api_base_url: '', token: '' })
    setEditingGitProviderId(null)
    setShowGitProviderForm(false)
  }

  const startEditGitProvider = (g: GitProviderConfig) => {
    setGitProviderForm({
      provider_type: g.provider_type,
      display_name: g.display_name,
      api_base_url: g.api_base_url || '',
      token: '',
    })
    setEditingGitProviderId(g.id)
    setShowGitProviderForm(true)
  }

  const handleSaveGitProvider = async () => {
    const data: Record<string, unknown> = {
      provider_type: gitProviderForm.provider_type,
      display_name: gitProviderForm.display_name,
      api_base_url: gitProviderForm.api_base_url || undefined,
    }
    if (gitProviderForm.token) {
      data.token = gitProviderForm.token
    }
    if (editingGitProviderId) {
      await gitProvidersApi.update(editingGitProviderId, data)
    } else {
      await gitProvidersApi.create(data as never)
    }
    resetGitProviderForm()
    const updated = await gitProvidersApi.list()
    setGitProviderList(updated as GitProviderConfig[])
  }

  const handleDeleteGitProvider = async (id: string) => {
    if (!confirm('Delete this git provider?')) return
    await gitProvidersApi.delete(id)
    setGitProviderList(gitProviderList.filter((g) => g.id !== id))
  }

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="text-sm font-medium text-gray-300 uppercase tracking-wider">Git Providers</h2>
          <p className="text-xs text-gray-600 mt-1">
            Configure access to GitHub, GitLab, Bitbucket, or self-hosted git servers.
          </p>
        </div>
        {!showGitProviderForm && (
          <button
            onClick={() => {
              resetGitProviderForm()
              setShowGitProviderForm(true)
            }}
            className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            Add git provider
          </button>
        )}
      </div>

      <div className="space-y-2 mb-3">
        {gitProviderList.map((g) => (
          <div key={g.id}>
            <div className="p-3 bg-gray-900 border border-gray-800 rounded-lg flex items-center justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-white font-medium">{g.display_name}</span>
                  <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500">
                    {g.provider_type}
                  </span>
                  {g.has_token && (
                    <span className="px-1.5 py-0.5 bg-green-900/30 rounded text-[10px] text-green-400">
                      token set
                    </span>
                  )}
                </div>
                {g.api_base_url && (
                  <div className="text-xs text-gray-500 mt-0.5 font-mono">{g.api_base_url}</div>
                )}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => startEditGitProvider(g)}
                  className="px-2 py-1 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
                >
                  Edit
                </button>
                <button onClick={() => handleDeleteGitProvider(g.id)} className={btnDanger}>
                  Delete
                </button>
              </div>
            </div>
            {/* Inline edit form */}
            {editingGitProviderId === g.id && showGitProviderForm && (
              <div className="mt-1 p-4 bg-gray-900 border border-indigo-900/50 rounded-lg space-y-3">
                <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">Edit Git Provider</div>
                <div className="grid grid-cols-2 gap-3">
                  <select
                    value={gitProviderForm.provider_type}
                    onChange={(e) => {
                      const type = e.target.value
                      setGitProviderForm({
                        ...gitProviderForm,
                        provider_type: type,
                        api_base_url: gitProviderDefaults[type] || '',
                      })
                    }}
                    className={inputClass}
                  >
                    <option value="github">GitHub</option>
                    <option value="gitlab">GitLab</option>
                    <option value="bitbucket">Bitbucket</option>
                    <option value="custom">Custom / Self-hosted</option>
                  </select>
                  <input
                    className={inputClass}
                    placeholder="Display name (e.g. Work GitHub)"
                    value={gitProviderForm.display_name}
                    onChange={(e) => setGitProviderForm({ ...gitProviderForm, display_name: e.target.value })}
                  />
                </div>
                <input
                  className={inputClass}
                  placeholder={`API Base URL (default: ${gitProviderDefaults[gitProviderForm.provider_type] || 'required for custom'})`}
                  value={gitProviderForm.api_base_url}
                  onChange={(e) => setGitProviderForm({ ...gitProviderForm, api_base_url: e.target.value })}
                />
                <input
                  type="password"
                  className={inputClass}
                  placeholder="Access token (leave blank to keep current)"
                  value={gitProviderForm.token}
                  onChange={(e) => setGitProviderForm({ ...gitProviderForm, token: e.target.value })}
                />
                <div className="flex gap-2">
                  <button onClick={handleSaveGitProvider} className={btnPrimary}>Update</button>
                  <button onClick={resetGitProviderForm} className={btnSecondary}>Cancel</button>
                </div>
              </div>
            )}
          </div>
        ))}
        {gitProviderList.length === 0 && (
          <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
            No git providers configured. Add one to enable private repo access.
          </div>
        )}
      </div>

      {/* Add new git provider form */}
      {showGitProviderForm && !editingGitProviderId && (
        <div className="p-4 bg-gray-900 border border-gray-800 rounded-lg space-y-3">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">New Git Provider</div>
          <div className="grid grid-cols-2 gap-3">
            <select
              value={gitProviderForm.provider_type}
              onChange={(e) => {
                const type = e.target.value
                setGitProviderForm({
                  ...gitProviderForm,
                  provider_type: type,
                  api_base_url: gitProviderDefaults[type] || '',
                })
              }}
              className={inputClass}
            >
              <option value="github">GitHub</option>
              <option value="gitlab">GitLab</option>
              <option value="bitbucket">Bitbucket</option>
              <option value="custom">Custom / Self-hosted</option>
            </select>
            <input
              className={inputClass}
              placeholder="Display name (e.g. Work GitHub)"
              value={gitProviderForm.display_name}
              onChange={(e) => setGitProviderForm({ ...gitProviderForm, display_name: e.target.value })}
            />
          </div>
          <input
            className={inputClass}
            placeholder={`API Base URL (default: ${gitProviderDefaults[gitProviderForm.provider_type] || 'required for custom'})`}
            value={gitProviderForm.api_base_url}
            onChange={(e) => setGitProviderForm({ ...gitProviderForm, api_base_url: e.target.value })}
          />
          <input
            type="password"
            className={inputClass}
            placeholder={
              gitProviderForm.provider_type === 'gitlab'
                ? 'Personal Access Token (read_repository, write_repository)'
                : gitProviderForm.provider_type === 'bitbucket'
                  ? 'App password (repository read/write)'
                  : 'Personal access token (repo scope)'
            }
            value={gitProviderForm.token}
            onChange={(e) => setGitProviderForm({ ...gitProviderForm, token: e.target.value })}
          />
          <div className="flex gap-2">
            <button onClick={handleSaveGitProvider} className={btnPrimary}>Save</button>
            <button onClick={resetGitProviderForm} className={btnSecondary}>Cancel</button>
          </div>
        </div>
      )}
    </section>
  )
}
