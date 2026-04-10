import { useState, useEffect } from 'react'
import { Loader2 } from 'lucide-react'
import { projects as projectsApi } from '../../services/api'
import type { ReleaseConfig, ReleaseEndpointConfig, BuildProviderConfig } from '../../types'
import { inputClass, selectClass } from '../../styles/classes'

interface ProjectReleaseTabProps {
  projectId: string
  userRole: string
  setError: (err: string) => void
}

const defaultEndpoint: ReleaseEndpointConfig = {
  enabled: false,
  api_url: '',
  http_method: 'POST',
  headers: {},
  body_template: '',
  success_status_codes: [200, 201, 202],
}

const defaultBuildConfig: BuildProviderConfig = {
  timeout_minutes: 30,
  poll_interval_seconds: 30,
}

const defaultReleaseConfig: ReleaseConfig = {
  build_provider: 'github_actions',
}

function HeadersEditor({ headers, onChange }: { headers: Record<string, string>; onChange: (h: Record<string, string>) => void }) {
  const entries = Object.entries(headers)
  return (
    <div className="space-y-1.5">
      {entries.map(([key, value], i) => (
        <div key={i} className="flex gap-1.5">
          <input className={`${inputClass} flex-1 font-mono text-xs`} placeholder="Header name" value={key}
            onChange={(e) => {
              const newH: Record<string, string> = {}
              entries.forEach(([k, v], j) => { newH[j === i ? e.target.value : k] = v })
              onChange(newH)
            }}
          />
          <input className={`${inputClass} flex-1 font-mono text-xs`} placeholder="Value" value={value}
            onChange={(e) => onChange({ ...headers, [key]: e.target.value })}
          />
          <button onClick={() => { const h = { ...headers }; delete h[key]; onChange(h) }}
            className="px-2 text-gray-600 hover:text-red-400 transition-colors text-xs shrink-0"
          >Remove</button>
        </div>
      ))}
      <button onClick={() => onChange({ ...headers, '': '' })} className="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors">
        + Add Header
      </button>
    </div>
  )
}

function EndpointSection({
  label, hint, config, onChange, showApproval, disabled,
}: {
  label: string; hint: string; config: ReleaseEndpointConfig
  onChange: (c: ReleaseEndpointConfig) => void; showApproval?: boolean; disabled?: boolean
}) {
  return (
    <div className="space-y-3 px-4 py-3 bg-gray-900 border border-gray-800 rounded-lg">
      <div className="flex items-center justify-between">
        <div>
          <span className="text-sm text-gray-300">{label}</span>
          <p className="text-[11px] text-gray-600">{hint}</p>
        </div>
        {!disabled && (
          <button
            onClick={() => onChange({ ...config, enabled: !config.enabled })}
            className={`relative w-9 h-5 rounded-full transition-colors ${config.enabled ? 'bg-indigo-600' : 'bg-gray-700'}`}
          >
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${config.enabled ? 'translate-x-4' : ''}`} />
          </button>
        )}
      </div>

      {config.enabled && (
        <div className="space-y-3 pt-1">
          <div className="grid grid-cols-[1fr_120px] gap-2">
            <div>
              <label className="block text-xs text-gray-500 mb-1">API URL</label>
              <input className={`${inputClass} font-mono text-xs`} placeholder="https://deploy.example.com/api/releases"
                value={config.api_url || ''} onChange={(e) => onChange({ ...config, api_url: e.target.value })} disabled={disabled}
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Method</label>
              <select value={config.http_method || 'POST'} onChange={(e) => onChange({ ...config, http_method: e.target.value })}
                className={selectClass} disabled={disabled}
              >
                <option value="POST">POST</option><option value="PUT">PUT</option><option value="PATCH">PATCH</option>
              </select>
            </div>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Headers</label>
            <HeadersEditor headers={config.headers || {}} onChange={(h) => onChange({ ...config, headers: h })} />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Request Body Template</label>
            <textarea className={`${inputClass} font-mono text-xs min-h-[80px]`}
              placeholder={'{"image": "{{image_hash}}", "sha": "{{commit_sha}}", "env": "{{env}}"}'}
              value={config.body_template || ''} onChange={(e) => onChange({ ...config, body_template: e.target.value })}
              rows={3} disabled={disabled}
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs text-gray-500 mb-1">Success Status Codes</label>
              <input className={`${inputClass} font-mono text-xs`} placeholder="200, 201, 202"
                value={(config.success_status_codes || []).join(', ')}
                onChange={(e) => onChange({ ...config, success_status_codes: e.target.value.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n)) })}
                disabled={disabled}
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Poll Success Value</label>
              <input className={`${inputClass} font-mono text-xs`} placeholder="deployed"
                value={config.poll_success_value || ''} onChange={(e) => onChange({ ...config, poll_success_value: e.target.value })} disabled={disabled}
              />
            </div>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Poll Status URL (optional)</label>
            <input className={`${inputClass} font-mono text-xs`} placeholder="https://deploy.example.com/status/{{release_id}}"
              value={config.poll_status_url || ''} onChange={(e) => onChange({ ...config, poll_status_url: e.target.value })} disabled={disabled}
            />
          </div>
          {showApproval && !disabled && (
            <label className="flex items-center gap-3 cursor-pointer pt-1">
              <button
                onClick={() => onChange({ ...config, require_approval: !config.require_approval })}
                className={`relative w-9 h-5 rounded-full transition-colors ${config.require_approval ? 'bg-amber-600' : 'bg-gray-700'}`}
              >
                <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${config.require_approval ? 'translate-x-4' : ''}`} />
              </button>
              <div>
                <span className="text-sm text-gray-300">Require approval before production release</span>
                <p className="text-[11px] text-gray-600">Pipeline will pause and wait for approval before deploying to production.</p>
              </div>
            </label>
          )}
        </div>
      )}
    </div>
  )
}

export default function ProjectReleaseTab({ projectId, userRole, setError }: ProjectReleaseTabProps) {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [releaseEnabled, setReleaseEnabled] = useState(false)
  const [configs, setConfigs] = useState<Record<string, ReleaseConfig>>({})
  const [repos, setRepos] = useState<Array<{ name: string; repo_url: string }>>([])
  const [selectedRepo, setSelectedRepo] = useState('main')
  const isOwner = userRole === 'owner'

  useEffect(() => {
    projectsApi.releaseSettings.get(projectId).then((res) => {
      setReleaseEnabled(res.release_pipeline_enabled)
      setConfigs(res.release_configs || {})
      setRepos(res.repos || [{ name: 'main', repo_url: '' }])
      if (res.repos?.length > 0) setSelectedRepo(res.repos[0].name)
    }).catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [projectId])

  const config = configs[selectedRepo] || defaultReleaseConfig
  const buildConfig = config.build_config || defaultBuildConfig
  const testRelease = config.test_release || defaultEndpoint
  const prodRelease = config.prod_release || { ...defaultEndpoint, require_approval: true }

  const updateConfig = (patch: Partial<ReleaseConfig>) => {
    setConfigs({ ...configs, [selectedRepo]: { ...config, ...patch } })
  }
  const updateBuildConfig = (updates: Partial<BuildProviderConfig>) => {
    updateConfig({ build_config: { ...buildConfig, ...updates } })
  }

  const save = async () => {
    setSaving(true)
    setError('')
    try {
      await projectsApi.releaseSettings.update(projectId, {
        release_pipeline_enabled: releaseEnabled,
        release_configs: releaseEnabled ? configs : undefined,
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save release settings')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-gray-500 text-sm py-8 justify-center">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading...
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Repo selector */}
      {repos.length > 1 && (
        <div className="flex items-center gap-3">
          <label className="text-xs text-gray-500">Repository</label>
          <div className="flex gap-1">
            {repos.map((r) => (
              <button key={r.name} onClick={() => setSelectedRepo(r.name)}
                className={`px-2.5 py-1 rounded-lg text-xs transition-colors ${
                  selectedRepo === r.name
                    ? 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20'
                    : 'text-gray-500 hover:text-gray-300 bg-gray-900 border border-gray-800'
                }`}
              >{r.name}</button>
            ))}
          </div>
        </div>
      )}

      <div>
        <p className="text-sm text-gray-300">
          Release Pipeline {repos.length > 1 ? <span className="text-gray-500 font-normal">— {selectedRepo}</span> : ''}
        </p>
        <p className="text-[11px] text-gray-600 mt-0.5">
          Automated build watching and deployment after task PRs are merged.
        </p>
      </div>

      {/* Enable toggle */}
      <div className="flex items-center justify-between">
        <span className="text-sm text-gray-300">Enable release pipeline</span>
        {isOwner && (
          <button onClick={() => setReleaseEnabled(!releaseEnabled)}
            className={`relative w-9 h-5 rounded-full transition-colors ${releaseEnabled ? 'bg-indigo-600' : 'bg-gray-700'}`}
          >
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${releaseEnabled ? 'translate-x-4' : ''}`} />
          </button>
        )}
      </div>

      {releaseEnabled && (
        <>
          {/* Build Provider */}
          <div>
            <label className="block text-xs text-gray-500 mb-1">Build Provider</label>
            <select value={config.build_provider || 'github_actions'}
              onChange={(e) => updateConfig({ build_provider: e.target.value as 'github_actions' | 'jenkins' })}
              className={selectClass} disabled={!isOwner}
            >
              <option value="github_actions">GitHub Actions</option>
              <option value="jenkins">Jenkins</option>
            </select>
          </div>

          {/* Build Config */}
          <div className="space-y-3 px-4 py-3 bg-gray-900 border border-gray-800 rounded-lg">
            <span className="text-sm text-gray-300">Build Configuration</span>
            {config.build_provider === 'github_actions' ? (
              <div>
                <label className="block text-xs text-gray-500 mb-1">Workflow Name</label>
                <input className={`${inputClass} font-mono text-xs`} placeholder="e.g. build-and-push"
                  value={buildConfig.workflow_name || ''} onChange={(e) => updateBuildConfig({ workflow_name: e.target.value })}
                  disabled={!isOwner}
                />
              </div>
            ) : (
              <>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Jenkins Job URL</label>
                  <input className={`${inputClass} font-mono text-xs`} placeholder="https://jenkins.example.com/job/my-build"
                    value={buildConfig.job_url || ''} onChange={(e) => updateBuildConfig({ job_url: e.target.value })}
                    disabled={!isOwner}
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Jenkins Token</label>
                  <input type="password" className={`${inputClass} font-mono text-xs`} placeholder="Bearer token"
                    value={buildConfig.token || ''} onChange={(e) => updateBuildConfig({ token: e.target.value })}
                    disabled={!isOwner}
                  />
                </div>
              </>
            )}
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="block text-xs text-gray-500 mb-1">Timeout (minutes)</label>
                <input type="number" className={`${inputClass} text-xs`} value={buildConfig.timeout_minutes || 30}
                  onChange={(e) => updateBuildConfig({ timeout_minutes: parseInt(e.target.value) || 30 })} disabled={!isOwner}
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Poll Interval (seconds)</label>
                <input type="number" className={`${inputClass} text-xs`} value={buildConfig.poll_interval_seconds || 30}
                  onChange={(e) => updateBuildConfig({ poll_interval_seconds: parseInt(e.target.value) || 30 })} disabled={!isOwner}
                />
              </div>
            </div>
          </div>

          <EndpointSection label="Staging Release" hint="Deploy to staging/test after build."
            config={testRelease} onChange={(c) => updateConfig({ test_release: c })} disabled={!isOwner}
          />
          <EndpointSection label="Production Release" hint="Deploy to production after staging succeeds."
            config={prodRelease} onChange={(c) => updateConfig({ prod_release: c })} showApproval disabled={!isOwner}
          />

          {/* Variable Reference */}
          <div className="px-4 py-3 bg-gray-900/50 border border-gray-800/50 rounded-lg">
            <span className="text-xs text-gray-500">Template Variables</span>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {['image_hash', 'commit_sha', 'env', 'project_name', 'todo_id', 'release_id'].map((v) => (
                <code key={v} className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-indigo-400 font-mono">
                  {'{{' + v + '}}'}
                </code>
              ))}
            </div>
          </div>
        </>
      )}

      {/* Save Button */}
      {isOwner && (
        <div className="pt-2">
          <button onClick={save} disabled={saving}
            className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
          >
            {saving ? 'Saving...' : 'Save Release Settings'}
          </button>
        </div>
      )}
    </div>
  )
}
