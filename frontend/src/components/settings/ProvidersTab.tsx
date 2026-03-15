import { useEffect, useState } from 'react'
import { providers as providersApi } from '../../services/api'
import type { ProviderConfig } from '../../types'
import { inputClass, btnPrimary, btnSecondary, btnDanger } from '../../styles/classes'

interface Props {
  isAdmin: boolean
}

const providerDefaults: Record<string, { model: string; fast: string }> = {
  anthropic: { model: 'claude-sonnet-4-20250514', fast: 'claude-haiku-3-20250311' },
  openai: { model: 'gpt-4o', fast: 'gpt-4o-mini' },
  self_hosted: { model: '', fast: '' },
}

export default function ProvidersTab({ isAdmin: _isAdmin }: Props) {
  const [providerList, setProviderList] = useState<ProviderConfig[]>([])
  const [showProviderForm, setShowProviderForm] = useState(false)
  const [editingProviderId, setEditingProviderId] = useState<string | null>(null)
  const [providerForm, setProviderForm] = useState({
    provider_type: 'anthropic',
    display_name: '',
    api_key: '',
    api_base_url: '',
    default_model: '',
    fast_model: '',
  })
  const [testResult, setTestResult] = useState<{ id: string; status: string; detail?: string } | null>(null)
  const [testingProviderId, setTestingProviderId] = useState<string | null>(null)

  useEffect(() => {
    providersApi.list().then((p) => setProviderList(p as ProviderConfig[]))
  }, [])

  const resetProviderForm = () => {
    setProviderForm({
      provider_type: 'anthropic',
      display_name: '',
      api_key: '',
      api_base_url: '',
      default_model: providerDefaults.anthropic.model,
      fast_model: providerDefaults.anthropic.fast,
    })
    setEditingProviderId(null)
    setShowProviderForm(false)
  }

  const startEditProvider = (p: ProviderConfig) => {
    setProviderForm({
      provider_type: p.provider_type,
      display_name: p.display_name,
      api_key: '',
      api_base_url: p.api_base_url || '',
      default_model: p.default_model,
      fast_model: p.fast_model || '',
    })
    setEditingProviderId(p.id)
    setShowProviderForm(true)
  }

  const handleSaveProvider = async () => {
    const data = {
      ...providerForm,
      api_base_url: providerForm.api_base_url || undefined,
      api_key: providerForm.api_key || undefined,
      fast_model: providerForm.fast_model || undefined,
    }
    if (editingProviderId) {
      await providersApi.update(editingProviderId, data)
    } else {
      await providersApi.create(data)
    }
    resetProviderForm()
    const updated = await providersApi.list()
    setProviderList(updated as ProviderConfig[])
  }

  const handleDeleteProvider = async (id: string) => {
    if (!confirm('Delete this provider?')) return
    await providersApi.delete(id)
    setProviderList(providerList.filter((p) => p.id !== id))
  }

  const handleTestProvider = async (id: string) => {
    setTestingProviderId(id)
    setTestResult(null)
    try {
      const result = await providersApi.test(id)
      setTestResult({ id, status: result.status, detail: result.detail })
    } catch (err) {
      setTestResult({ id, status: 'error', detail: err instanceof Error ? err.message : 'Request failed' })
    } finally {
      setTestingProviderId(null)
    }
  }

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-medium text-gray-300 uppercase tracking-wider">AI Providers</h2>
        {!showProviderForm && (
          <button
            onClick={() => {
              resetProviderForm()
              setShowProviderForm(true)
            }}
            className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            Add provider
          </button>
        )}
      </div>

      <div className="space-y-2 mb-3">
        {providerList.map((p) => (
          <div key={p.id}>
            <div className="p-3 bg-gray-900 border border-gray-800 rounded-lg">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm text-white font-medium">{p.display_name}</div>
                  <div className="text-xs text-gray-500">
                    {p.provider_type} / {p.default_model}
                    {p.fast_model && ` / fast: ${p.fast_model}`}
                  </div>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => handleTestProvider(p.id)}
                    disabled={testingProviderId === p.id}
                    className="px-2 py-1 text-xs text-gray-400 hover:text-white transition-colors disabled:opacity-50"
                  >
                    {testingProviderId === p.id ? 'Testing...' : 'Test'}
                  </button>
                  <button
                    onClick={() => startEditProvider(p)}
                    className="px-2 py-1 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
                  >
                    Edit
                  </button>
                  <button onClick={() => handleDeleteProvider(p.id)} className={btnDanger}>
                    Delete
                  </button>
                </div>
              </div>
              {testResult && testResult.id === p.id && (
                <div
                  className={`mt-2 px-3 py-2 rounded text-xs ${
                    testResult.status === 'ok'
                      ? 'bg-green-900/30 text-green-400 border border-green-800/50'
                      : 'bg-red-900/30 text-red-400 border border-red-800/50'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium">
                      {testResult.status === 'ok' ? 'Connection successful' : 'Connection failed'}
                    </span>
                    <button
                      onClick={() => setTestResult(null)}
                      className="text-gray-500 hover:text-gray-300 ml-2"
                    >
                      x
                    </button>
                  </div>
                  {testResult.detail && (
                    <div className="mt-1 text-[11px] opacity-80 font-mono break-all">{testResult.detail}</div>
                  )}
                </div>
              )}
            </div>
            {/* Inline edit form */}
            {editingProviderId === p.id && showProviderForm && (
              <div className="mt-1 p-4 bg-gray-900 border border-indigo-900/50 rounded-lg space-y-3">
                <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">Edit Provider</div>
                <select
                  value={providerForm.provider_type}
                  onChange={(e) => {
                    const type = e.target.value
                    setProviderForm({
                      ...providerForm,
                      provider_type: type,
                      default_model: providerDefaults[type]?.model || providerForm.default_model,
                      fast_model: providerDefaults[type]?.fast || providerForm.fast_model,
                    })
                  }}
                  className={inputClass}
                >
                  <option value="anthropic">Anthropic (Claude)</option>
                  <option value="openai">OpenAI</option>
                  <option value="self_hosted">Self-Hosted</option>
                </select>
                <input
                  className={inputClass}
                  placeholder="Display name"
                  value={providerForm.display_name}
                  onChange={(e) => setProviderForm({ ...providerForm, display_name: e.target.value })}
                />
                <input
                  className={inputClass}
                  placeholder="API Key (leave blank to keep current)"
                  type="password"
                  value={providerForm.api_key}
                  onChange={(e) => setProviderForm({ ...providerForm, api_key: e.target.value })}
                />
                {providerForm.provider_type === 'self_hosted' && (
                  <input
                    className={inputClass}
                    placeholder="API Base URL (e.g., http://localhost:11434/v1)"
                    value={providerForm.api_base_url}
                    onChange={(e) => setProviderForm({ ...providerForm, api_base_url: e.target.value })}
                  />
                )}
                <div className="grid grid-cols-2 gap-3">
                  <input
                    className={inputClass}
                    placeholder="Default model"
                    value={providerForm.default_model}
                    onChange={(e) => setProviderForm({ ...providerForm, default_model: e.target.value })}
                  />
                  <input
                    className={inputClass}
                    placeholder="Fast model (optional)"
                    value={providerForm.fast_model}
                    onChange={(e) => setProviderForm({ ...providerForm, fast_model: e.target.value })}
                  />
                </div>
                <div className="flex gap-2">
                  <button onClick={handleSaveProvider} className={btnPrimary}>Update</button>
                  <button onClick={resetProviderForm} className={btnSecondary}>Cancel</button>
                </div>
              </div>
            )}
          </div>
        ))}
        {providerList.length === 0 && (
          <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
            No providers configured
          </div>
        )}
      </div>

      {/* Add new provider form */}
      {showProviderForm && !editingProviderId && (
        <div className="p-4 bg-gray-900 border border-gray-800 rounded-lg space-y-3">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">New Provider</div>
          <select
            value={providerForm.provider_type}
            onChange={(e) => {
              const type = e.target.value
              setProviderForm({
                ...providerForm,
                provider_type: type,
                default_model: providerDefaults[type]?.model || providerForm.default_model,
                fast_model: providerDefaults[type]?.fast || providerForm.fast_model,
              })
            }}
            className={inputClass}
          >
            <option value="anthropic">Anthropic (Claude)</option>
            <option value="openai">OpenAI</option>
            <option value="self_hosted">Self-Hosted</option>
          </select>
          <input
            className={inputClass}
            placeholder="Display name"
            value={providerForm.display_name}
            onChange={(e) => setProviderForm({ ...providerForm, display_name: e.target.value })}
          />
          <input
            className={inputClass}
            placeholder="API Key"
            type="password"
            value={providerForm.api_key}
            onChange={(e) => setProviderForm({ ...providerForm, api_key: e.target.value })}
          />
          {providerForm.provider_type === 'self_hosted' && (
            <input
              className={inputClass}
              placeholder="API Base URL (e.g., http://localhost:11434/v1)"
              value={providerForm.api_base_url}
              onChange={(e) => setProviderForm({ ...providerForm, api_base_url: e.target.value })}
            />
          )}
          <div className="grid grid-cols-2 gap-3">
            <input
              className={inputClass}
              placeholder="Default model"
              value={providerForm.default_model}
              onChange={(e) => setProviderForm({ ...providerForm, default_model: e.target.value })}
            />
            <input
              className={inputClass}
              placeholder="Fast model (optional)"
              value={providerForm.fast_model}
              onChange={(e) => setProviderForm({ ...providerForm, fast_model: e.target.value })}
            />
          </div>
          <div className="flex gap-2">
            <button onClick={handleSaveProvider} className={btnPrimary}>Save</button>
            <button onClick={resetProviderForm} className={btnSecondary}>Cancel</button>
          </div>
        </div>
      )}
    </section>
  )
}
