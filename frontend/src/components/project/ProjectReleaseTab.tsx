import { useState } from 'react'
import { projects as projectsApi } from '../../services/api'
import type { ReleaseConfig, ReleaseEndpointConfig, BuildProviderConfig } from '../../types'
import { inputClass, selectClass } from '../../styles/classes'

interface ProjectReleaseTabProps {
  projectId: string
  releaseEnabled: boolean
  setReleaseEnabled: (v: boolean) => void
  releaseConfig: ReleaseConfig
  setReleaseConfig: (c: ReleaseConfig) => void
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

function HeadersEditor({ headers, onChange }: { headers: Record<string, string>; onChange: (h: Record<string, string>) => void }) {
  const entries = Object.entries(headers)
  return (
    <div className="space-y-1.5">
      {entries.map(([key, value], i) => (
        <div key={i} className="flex gap-1.5">
          <input
            className={`${inputClass} flex-1 font-mono text-xs`}
            placeholder="Header name"
            value={key}
            onChange={(e) => {
              const newHeaders: Record<string, string> = {}
              entries.forEach(([k, v], j) => {
                newHeaders[j === i ? e.target.value : k] = v
              })
              onChange(newHeaders)
            }}
          />
          <input
            className={`${inputClass} flex-1 font-mono text-xs`}
            placeholder="Value"
            value={value}
            onChange={(e) => {
              onChange({ ...headers, [key]: e.target.value })
            }}
          />
          <button
            onClick={() => {
              const newHeaders = { ...headers }
              delete newHeaders[key]
              onChange(newHeaders)
            }}
            className="px-2 text-gray-600 hover:text-red-400 transition-colors text-xs shrink-0"
          >
            Remove
          </button>
        </div>
      ))}
      <button
        onClick={() => onChange({ ...headers, '': '' })}
        className="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors"
      >
        + Add Header
      </button>
    </div>
  )
}

function EndpointSection({
  label, hint, config, onChange, showApproval,
}: {
  label: string
  hint: string
  config: ReleaseEndpointConfig
  onChange: (c: ReleaseEndpointConfig) => void
  showApproval?: boolean
}) {
  return (
    <div className="space-y-3 px-4 py-3 bg-gray-900 border border-gray-800 rounded-lg">
      <div className="flex items-center justify-between">
        <div>
          <span className="text-sm text-gray-300">{label}</span>
          <p className="text-[11px] text-gray-600">{hint}</p>
        </div>
        <label className="relative cursor-pointer">
          <input
            type="checkbox"
            checked={config.enabled}
            onChange={(e) => onChange({ ...config, enabled: e.target.checked })}
            className="sr-only peer"
          />
          <div className="w-9 h-5 bg-gray-800 border border-gray-700 rounded-full peer-checked:bg-indigo-600 peer-checked:border-indigo-500 transition-colors" />
          <div className="absolute top-0.5 left-0.5 w-4 h-4 bg-gray-500 rounded-full peer-checked:translate-x-4 peer-checked:bg-white transition-all" />
        </label>
      </div>

      {config.enabled && (
        <div className="space-y-3 pt-1">
          <div className="grid grid-cols-[1fr_120px] gap-2">
            <div>
              <label className="block text-xs text-gray-500 mb-1">API URL</label>
              <input
                className={`${inputClass} font-mono text-xs`}
                placeholder="https://deploy.example.com/api/releases"
                value={config.api_url || ''}
                onChange={(e) => onChange({ ...config, api_url: e.target.value })}
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Method</label>
              <select
                value={config.http_method || 'POST'}
                onChange={(e) => onChange({ ...config, http_method: e.target.value })}
                className={selectClass}
              >
                <option value="POST">POST</option>
                <option value="PUT">PUT</option>
                <option value="PATCH">PATCH</option>
              </select>
            </div>
          </div>

          <div>
            <label className="block text-xs text-gray-500 mb-1">Headers</label>
            <HeadersEditor
              headers={config.headers || {}}
              onChange={(h) => onChange({ ...config, headers: h })}
            />
          </div>

          <div>
            <label className="block text-xs text-gray-500 mb-1">Request Body Template</label>
            <textarea
              className={`${inputClass} font-mono text-xs min-h-[80px]`}
              placeholder={'{"image": "{{image_hash}}", "sha": "{{commit_sha}}", "env": "{{env}}"}'}
              value={config.body_template || ''}
              onChange={(e) => onChange({ ...config, body_template: e.target.value })}
              rows={3}
            />
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs text-gray-500 mb-1">Success Status Codes</label>
              <input
                className={`${inputClass} font-mono text-xs`}
                placeholder="200, 201, 202"
                value={(config.success_status_codes || []).join(', ')}
                onChange={(e) => onChange({
                  ...config,
                  success_status_codes: e.target.value.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n)),
                })}
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Poll Success Value</label>
              <input
                className={`${inputClass} font-mono text-xs`}
                placeholder="deployed"
                value={config.poll_success_value || ''}
                onChange={(e) => onChange({ ...config, poll_success_value: e.target.value })}
              />
            </div>
          </div>

          <div>
            <label className="block text-xs text-gray-500 mb-1">Poll Status URL (optional)</label>
            <input
              className={`${inputClass} font-mono text-xs`}
              placeholder="https://deploy.example.com/status/{{release_id}}"
              value={config.poll_status_url || ''}
              onChange={(e) => onChange({ ...config, poll_status_url: e.target.value })}
            />
            <p className="text-[11px] text-gray-700 mt-0.5">If set, the system will poll this URL until it returns the success value.</p>
          </div>

          {showApproval && (
            <label className="flex items-center gap-3 cursor-pointer pt-1">
              <div className="relative">
                <input
                  type="checkbox"
                  checked={config.require_approval || false}
                  onChange={(e) => onChange({ ...config, require_approval: e.target.checked })}
                  className="sr-only peer"
                />
                <div className="w-9 h-5 bg-gray-800 border border-gray-700 rounded-full peer-checked:bg-amber-600 peer-checked:border-amber-500 transition-colors" />
                <div className="absolute top-0.5 left-0.5 w-4 h-4 bg-gray-500 rounded-full peer-checked:translate-x-4 peer-checked:bg-white transition-all" />
              </div>
              <div>
                <span className="text-sm text-gray-300">Require approval before production release</span>
                <p className="text-[11px] text-gray-600">When enabled, the pipeline will pause and wait for your approval before deploying to production.</p>
              </div>
            </label>
          )}
        </div>
      )}
    </div>
  )
}

export default function ProjectReleaseTab({
  projectId, releaseEnabled, setReleaseEnabled,
  releaseConfig, setReleaseConfig, setError,
}: ProjectReleaseTabProps) {
  const [saving, setSaving] = useState(false)

  const buildConfig = releaseConfig.build_config || defaultBuildConfig
  const testRelease = releaseConfig.test_release || defaultEndpoint
  const prodRelease = releaseConfig.prod_release || { ...defaultEndpoint, require_approval: true }

  const updateBuildConfig = (updates: Partial<BuildProviderConfig>) => {
    setReleaseConfig({ ...releaseConfig, build_config: { ...buildConfig, ...updates } })
  }

  return (
    <>
      <div>
        <p className="text-sm text-gray-300">Release Pipeline</p>
        <p className="text-[11px] text-gray-600 mt-0.5">
          Configure automated build watching and deployment after PR merge.
        </p>
      </div>

      {/* Enable toggle */}
      <div>
        <label className="flex items-center gap-3 cursor-pointer group">
          <div className="relative">
            <input
              type="checkbox"
              checked={releaseEnabled}
              onChange={(e) => setReleaseEnabled(e.target.checked)}
              className="sr-only peer"
            />
            <div className="w-9 h-5 bg-gray-800 border border-gray-700 rounded-full peer-checked:bg-indigo-600 peer-checked:border-indigo-500 transition-colors" />
            <div className="absolute top-0.5 left-0.5 w-4 h-4 bg-gray-500 rounded-full peer-checked:translate-x-4 peer-checked:bg-white transition-all" />
          </div>
          <div>
            <span className="text-sm text-gray-300 group-hover:text-white transition-colors">
              Enable release pipeline
            </span>
            <p className="text-[11px] text-gray-600">
              After a PR is merged, automatically watch the build, then deploy to staging and production.
            </p>
          </div>
        </label>
      </div>

      {releaseEnabled && (
        <>
          {/* Build Provider */}
          <div>
            <label className="block text-xs text-gray-500 mb-1">Build Provider</label>
            <select
              value={releaseConfig.build_provider || 'github_actions'}
              onChange={(e) => setReleaseConfig({ ...releaseConfig, build_provider: e.target.value as 'github_actions' | 'jenkins' })}
              className={selectClass}
            >
              <option value="github_actions">GitHub Actions</option>
              <option value="jenkins">Jenkins</option>
            </select>
            <p className="text-[11px] text-gray-600 mt-1">
              The CI/CD system that builds Docker images after merge.
            </p>
          </div>

          {/* Build Config */}
          <div className="space-y-3 px-4 py-3 bg-gray-900 border border-gray-800 rounded-lg">
            <span className="text-sm text-gray-300">Build Configuration</span>

            {releaseConfig.build_provider === 'github_actions' ? (
              <div>
                <label className="block text-xs text-gray-500 mb-1">Workflow Name</label>
                <input
                  className={`${inputClass} font-mono text-xs`}
                  placeholder="e.g. build-and-push"
                  value={buildConfig.workflow_name || ''}
                  onChange={(e) => updateBuildConfig({ workflow_name: e.target.value })}
                />
                <p className="text-[11px] text-gray-600 mt-1">
                  The name of the GitHub Actions workflow to watch. Must match the workflow's `name:` field.
                </p>
              </div>
            ) : (
              <>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Jenkins Job URL</label>
                  <input
                    className={`${inputClass} font-mono text-xs`}
                    placeholder="https://jenkins.example.com/job/my-build"
                    value={buildConfig.job_url || ''}
                    onChange={(e) => updateBuildConfig({ job_url: e.target.value })}
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Jenkins Token</label>
                  <input
                    type="password"
                    className={`${inputClass} font-mono text-xs`}
                    placeholder="Bearer token for Jenkins API"
                    value={buildConfig.token || ''}
                    onChange={(e) => updateBuildConfig({ token: e.target.value })}
                  />
                </div>
              </>
            )}

            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="block text-xs text-gray-500 mb-1">Timeout (minutes)</label>
                <input
                  type="number"
                  className={`${inputClass} text-xs`}
                  value={buildConfig.timeout_minutes || 30}
                  onChange={(e) => updateBuildConfig({ timeout_minutes: parseInt(e.target.value) || 30 })}
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Poll Interval (seconds)</label>
                <input
                  type="number"
                  className={`${inputClass} text-xs`}
                  value={buildConfig.poll_interval_seconds || 30}
                  onChange={(e) => updateBuildConfig({ poll_interval_seconds: parseInt(e.target.value) || 30 })}
                />
              </div>
            </div>
          </div>

          {/* Staging Release */}
          <EndpointSection
            label="Staging Release"
            hint="Configure the API call to deploy to your staging/test environment after build."
            config={testRelease}
            onChange={(c) => setReleaseConfig({ ...releaseConfig, test_release: c })}
          />

          {/* Production Release */}
          <EndpointSection
            label="Production Release"
            hint="Configure the API call to deploy to production after staging succeeds."
            config={prodRelease}
            onChange={(c) => setReleaseConfig({ ...releaseConfig, prod_release: c })}
            showApproval
          />

          {/* Variable Reference */}
          <div className="px-4 py-3 bg-gray-900/50 border border-gray-800/50 rounded-lg">
            <span className="text-xs text-gray-500">Template Variables</span>
            <p className="text-[11px] text-gray-600 mt-1 leading-relaxed">
              Use these in URLs, headers, and body templates:
            </p>
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
      <div className="pt-2">
        <button
          onClick={async () => {
            if (!projectId) return
            setSaving(true)
            setError('')
            try {
              await projectsApi.releaseSettings.update(projectId, {
                release_pipeline_enabled: releaseEnabled,
                release_config: releaseEnabled ? releaseConfig : undefined,
              })
            } catch (e) {
              setError(e instanceof Error ? e.message : 'Failed to save release settings')
            } finally {
              setSaving(false)
            }
          }}
          disabled={saving}
          className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
        >
          {saving ? 'Saving...' : 'Save Release Settings'}
        </button>
      </div>
    </>
  )
}
