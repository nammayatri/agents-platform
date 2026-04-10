import { useState, useEffect } from 'react'
import { Loader2, Plus, Trash2, X } from 'lucide-react'
import { projects as projectsApi } from '../../services/api'
import type { MergePipelineConfig, MergePipelineTestConfig, MergePipelineDeployConfig, PipelineVariable, PostMergeRepoConfig, PostMergeAction } from '../../types'
import { inputClass, selectClass } from '../../styles/classes'

interface Props {
  projectId: string
  userRole: string
  setError: (err: string) => void
}

const defaultTestConfig: MergePipelineTestConfig = {
  mode: 'poll',
  poll_url: '',
  poll_interval_seconds: 10,
  poll_timeout_minutes: 15,
  poll_success_value: 'passed',
  timeout_minutes: 30,
}

const defaultDeployConfig: MergePipelineDeployConfig = {
  enabled: false,
  deploy_type: 'http',
  api_url: '',
  http_method: 'POST',
  headers: {},
  body_template: '',
  success_status_codes: [200, 201, 202],
  kube_commands: [],
}

const emptyConfig: MergePipelineConfig = { enabled: false }

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
          <input className={`${inputClass} flex-1 font-mono text-xs`} placeholder="Value (supports {{variables}})" value={value}
            onChange={(e) => onChange({ ...headers, [key]: e.target.value })}
          />
          <button onClick={() => { const h = { ...headers }; delete h[key]; onChange(h) }} className="text-gray-600 hover:text-red-400 px-1">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}
      <button onClick={() => onChange({ ...headers, '': '' })} className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-300">
        <Plus className="w-3 h-3" /> Add header
      </button>
    </div>
  )
}

export default function ProjectMergePipelineTab({ projectId, userRole, setError }: Props) {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [configs, setConfigs] = useState<Record<string, MergePipelineConfig>>({})
  const [postMergeConfigs, setPostMergeConfigs] = useState<Record<string, PostMergeRepoConfig>>({})
  const [repos, setRepos] = useState<Array<{ name: string; repo_url: string }>>([])
  const [selectedRepo, setSelectedRepo] = useState('main')
  const [variables, setVariables] = useState<PipelineVariable[]>([])
  const isOwner = userRole === 'owner'

  useEffect(() => {
    Promise.all([
      projectsApi.mergePipeline.getSettings(projectId),
      projectsApi.mergePipeline.getVariables(projectId),
      projectsApi.postMergeActions.get(projectId),
    ]).then(([settingsRes, varsRes, actionsRes]) => {
      setConfigs(settingsRes.merge_pipelines || {})
      setRepos(settingsRes.repos || [{ name: 'main', repo_url: '' }])
      setVariables(varsRes.variables || [])
      setPostMergeConfigs(actionsRes.post_merge_actions || {})
      if (settingsRes.repos?.length > 0) setSelectedRepo(settingsRes.repos[0].name)
    }).catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [projectId])

  const config = configs[selectedRepo] || emptyConfig
  const testConfig = config.test_config || defaultTestConfig
  const deployConfig = config.deploy_config || defaultDeployConfig

  const updateConfig = (patch: Partial<MergePipelineConfig>) => {
    setConfigs({ ...configs, [selectedRepo]: { ...config, ...patch } })
  }
  const updateTest = (patch: Partial<MergePipelineTestConfig>) => {
    updateConfig({ test_config: { ...testConfig, ...patch } })
  }
  const updateDeploy = (patch: Partial<MergePipelineDeployConfig>) => {
    updateConfig({ deploy_config: { ...deployConfig, ...patch } })
  }

  const save = async () => {
    setSaving(true)
    try {
      const [pipelineRes, actionsRes] = await Promise.all([
        projectsApi.mergePipeline.updateSettings(projectId, { merge_pipelines: configs }),
        projectsApi.postMergeActions.update(projectId, { post_merge_actions: postMergeConfigs }),
      ])
      setConfigs(pipelineRes.merge_pipelines)
      setPostMergeConfigs(actionsRes.post_merge_actions)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  // Post-merge action helpers
  const pmConfig = postMergeConfigs[selectedRepo] || { enabled: false, actions: [] }
  const pmActions = pmConfig.actions || []

  const updatePmConfig = (patch: Partial<PostMergeRepoConfig>) => {
    setPostMergeConfigs({ ...postMergeConfigs, [selectedRepo]: { ...pmConfig, ...patch } })
  }
  const updatePmAction = (idx: number, patch: Partial<PostMergeAction>) => {
    const updated = pmActions.map((a, i) => i === idx ? { ...a, ...patch } : a)
    updatePmConfig({ actions: updated })
  }
  const addPmAction = (type: 'webhook' | 'script') => {
    const action: PostMergeAction = type === 'webhook'
      ? { type: 'webhook', url: '', method: 'POST', timeout_seconds: 30 }
      : { type: 'script', command: '', timeout_seconds: 120 }
    updatePmConfig({ actions: [...pmActions, action] })
  }
  const removePmAction = (idx: number) => {
    updatePmConfig({ actions: pmActions.filter((_, i) => i !== idx) })
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
              <button
                key={r.name}
                onClick={() => setSelectedRepo(r.name)}
                className={`px-2.5 py-1 rounded-lg text-xs transition-colors ${
                  selectedRepo === r.name
                    ? 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20'
                    : 'text-gray-500 hover:text-gray-300 bg-gray-900 border border-gray-800'
                }`}
              >
                {r.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Enable toggle */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm text-white font-medium">
            Merge Pipeline {repos.length > 1 ? <span className="text-gray-500 font-normal">— {selectedRepo}</span> : ''}
          </h3>
          <p className="text-xs text-gray-500 mt-0.5">
            Run tests and deploy when PRs are merged to <span className="text-gray-400 font-mono">{repos.find((r) => r.name === selectedRepo)?.repo_url || selectedRepo}</span>
          </p>
        </div>
        {isOwner && (
          <button
            onClick={() => updateConfig({ enabled: !config.enabled })}
            className={`relative w-9 h-5 rounded-full transition-colors ${config.enabled ? 'bg-indigo-600' : 'bg-gray-700'}`}
          >
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${config.enabled ? 'translate-x-4' : ''}`} />
          </button>
        )}
      </div>

      {config.enabled && (
        <>
          {/* Test Phase */}
          <section className="space-y-3">
            <h4 className="text-xs text-gray-400 uppercase tracking-wider">Test Phase</h4>

            <div>
              <label className="text-xs text-gray-500 block mb-1">Test result source</label>
              <select className={selectClass} value={testConfig.mode}
                onChange={(e) => updateTest({ mode: e.target.value as 'poll' | 'webhook' })} disabled={!isOwner}
              >
                <option value="poll">Poll an API endpoint</option>
                <option value="webhook">Wait for webhook callback</option>
              </select>
            </div>

            {testConfig.mode === 'poll' ? (
              <div className="space-y-2.5 pl-3 border-l border-gray-800">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Poll URL <span className="text-gray-700">(supports {'{{variables}}'})</span></label>
                  <input className={inputClass} placeholder="https://ci.example.com/api/runs/{{commit_hash}}/status"
                    value={testConfig.poll_url || ''} onChange={(e) => updateTest({ poll_url: e.target.value })} disabled={!isOwner}
                  />
                </div>
                <div className="grid grid-cols-3 gap-2">
                  <div>
                    <label className="text-xs text-gray-500 block mb-1">Poll interval (s)</label>
                    <input type="number" className={inputClass} value={testConfig.poll_interval_seconds ?? 10}
                      onChange={(e) => updateTest({ poll_interval_seconds: +e.target.value })} disabled={!isOwner}
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-500 block mb-1">Timeout (min)</label>
                    <input type="number" className={inputClass} value={testConfig.poll_timeout_minutes ?? 15}
                      onChange={(e) => updateTest({ poll_timeout_minutes: +e.target.value })} disabled={!isOwner}
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-500 block mb-1">Success value</label>
                    <input className={inputClass} value={testConfig.poll_success_value || 'passed'}
                      onChange={(e) => updateTest({ poll_success_value: e.target.value })} disabled={!isOwner}
                    />
                  </div>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Poll headers</label>
                  <HeadersEditor headers={testConfig.poll_headers || {}} onChange={(h) => updateTest({ poll_headers: h })} />
                </div>
              </div>
            ) : (
              <div className="pl-3 border-l border-gray-800">
                <div className="px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg">
                  <p className="text-xs text-gray-400">A unique webhook URL is generated for each pipeline run. Configure your CI to POST test results to it.</p>
                  <p className="text-xs text-gray-600 mt-1.5 font-mono">POST /api/webhooks/pipeline-test/{'<token>'}</p>
                  <p className="text-xs text-gray-600 mt-1">Body: {'{ "passed": true }'} or {'{ "passed": false }'}</p>
                </div>
                <div className="mt-2">
                  <label className="text-xs text-gray-500 block mb-1">Webhook timeout (min)</label>
                  <input type="number" className={`${inputClass} w-24`} value={testConfig.timeout_minutes ?? 30}
                    onChange={(e) => updateTest({ timeout_minutes: +e.target.value })} disabled={!isOwner}
                  />
                </div>
              </div>
            )}
          </section>

          {/* Deploy Phase */}
          <section className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <h4 className="text-xs text-gray-400 uppercase tracking-wider">Auto-Deploy to Test Environment</h4>
                <p className="text-[11px] text-gray-600 mt-0.5">Triggered automatically when tests pass</p>
              </div>
              {isOwner && (
                <button onClick={() => updateDeploy({ enabled: !deployConfig.enabled })}
                  className={`relative w-9 h-5 rounded-full transition-colors ${deployConfig.enabled ? 'bg-indigo-600' : 'bg-gray-700'}`}
                >
                  <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${deployConfig.enabled ? 'translate-x-4' : ''}`} />
                </button>
              )}
            </div>

            {deployConfig.enabled && (
              <div className="space-y-3 pl-3 border-l border-gray-800">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Deploy type</label>
                  <select className={selectClass} value={deployConfig.deploy_type}
                    onChange={(e) => updateDeploy({ deploy_type: e.target.value as 'http' | 'kubernetes' })} disabled={!isOwner}
                  >
                    <option value="http">HTTP API Call</option>
                    <option value="kubernetes">Kubernetes Commands</option>
                  </select>
                </div>

                {deployConfig.deploy_type === 'http' ? (
                  <div className="space-y-2.5">
                    <div className="flex gap-2">
                      <div className="w-24">
                        <label className="text-xs text-gray-500 block mb-1">Method</label>
                        <select className={selectClass} value={deployConfig.http_method || 'POST'}
                          onChange={(e) => updateDeploy({ http_method: e.target.value })} disabled={!isOwner}
                        >
                          <option>POST</option><option>PUT</option><option>PATCH</option>
                        </select>
                      </div>
                      <div className="flex-1">
                        <label className="text-xs text-gray-500 block mb-1">URL</label>
                        <input className={inputClass} placeholder="https://deploy.example.com/api/deploy"
                          value={deployConfig.api_url || ''} onChange={(e) => updateDeploy({ api_url: e.target.value })} disabled={!isOwner}
                        />
                      </div>
                    </div>
                    <div>
                      <label className="text-xs text-gray-500 block mb-1">Headers</label>
                      <HeadersEditor headers={deployConfig.headers || {}} onChange={(h) => updateDeploy({ headers: h })} />
                    </div>
                    <div>
                      <label className="text-xs text-gray-500 block mb-1">Body template <span className="text-gray-700">(supports {'{{variables}}'})</span></label>
                      <textarea className={`${inputClass} font-mono text-xs`} rows={4}
                        placeholder={'{\n  "image": "{{commit_hash}}",\n  "branch": "{{branch_name}}"\n}'}
                        value={deployConfig.body_template || ''} onChange={(e) => updateDeploy({ body_template: e.target.value })} disabled={!isOwner}
                      />
                    </div>
                    <div>
                      <label className="text-xs text-gray-500 block mb-1">Success status codes</label>
                      <input className={inputClass} placeholder="200, 201, 202"
                        value={(deployConfig.success_status_codes || [200, 201, 202]).join(', ')}
                        onChange={(e) => updateDeploy({ success_status_codes: e.target.value.split(',').map((s) => parseInt(s.trim())).filter(Boolean) })}
                        disabled={!isOwner}
                      />
                    </div>
                  </div>
                ) : (
                  <div className="space-y-2.5">
                    <div>
                      <label className="text-xs text-gray-500 block mb-1">Kubectl commands <span className="text-gray-700">(one per line, supports {'{{variables}}'})</span></label>
                      <textarea className={`${inputClass} font-mono text-xs`} rows={5}
                        placeholder={`kubectl set image deployment/myapp myapp=myrepo:{{commit_hash}}\nkubectl rollout status deployment/myapp`}
                        value={(deployConfig.kube_commands || []).join('\n')}
                        onChange={(e) => updateDeploy({ kube_commands: e.target.value.split('\n').filter(Boolean) })} disabled={!isOwner}
                      />
                    </div>
                    <div>
                      <label className="text-xs text-gray-500 block mb-1">Kube context (optional)</label>
                      <input className={inputClass} placeholder="my-cluster-context"
                        value={deployConfig.kube_context || ''} onChange={(e) => updateDeploy({ kube_context: e.target.value })} disabled={!isOwner}
                      />
                    </div>
                  </div>
                )}
              </div>
            )}
          </section>

        </>
      )}

      {/* ── Post-Merge Actions ──────────────────────────────────── */}
      <div className="pt-4 border-t border-gray-800">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm text-white font-medium">
              Post-Merge Actions {repos.length > 1 ? <span className="text-gray-500 font-normal">— {selectedRepo}</span> : ''}
            </h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Fire webhooks or run scripts automatically when a PR is merged. Runs independently of the pipeline above.
            </p>
          </div>
          {isOwner && (
            <button
              onClick={() => updatePmConfig({ enabled: !pmConfig.enabled })}
              className={`relative w-9 h-5 rounded-full transition-colors ${pmConfig.enabled ? 'bg-indigo-600' : 'bg-gray-700'}`}
            >
              <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${pmConfig.enabled ? 'translate-x-4' : ''}`} />
            </button>
          )}
        </div>

        {pmConfig.enabled && (
          <div className="mt-4 space-y-3">
            {pmActions.map((action, i) => (
              <div key={i} className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 space-y-2.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <select
                      className={`${selectClass} w-auto text-xs`}
                      value={action.type}
                      onChange={(e) => updatePmAction(i, { type: e.target.value as 'webhook' | 'script' })}
                      disabled={!isOwner}
                    >
                      <option value="webhook">Webhook</option>
                      <option value="script">Script</option>
                    </select>
                    <span className="text-[11px] text-gray-600">#{i + 1}</span>
                  </div>
                  {isOwner && (
                    <button onClick={() => removePmAction(i)} className="text-gray-600 hover:text-red-400 transition-colors">
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>

                {action.type === 'webhook' ? (
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <select className={`${selectClass} w-24 text-xs shrink-0`} value={action.method || 'POST'}
                        onChange={(e) => updatePmAction(i, { method: e.target.value })} disabled={!isOwner}>
                        <option value="POST">POST</option>
                        <option value="GET">GET</option>
                        <option value="PUT">PUT</option>
                      </select>
                      <input className={`${inputClass} flex-1 font-mono text-xs`} placeholder="https://jenkins.example.com/job/build/buildWithParameters"
                        value={action.url || ''} onChange={(e) => updatePmAction(i, { url: e.target.value })} disabled={!isOwner}
                      />
                    </div>
                    <div>
                      <label className="text-[11px] text-gray-600 block mb-1">Headers</label>
                      <HeadersEditor headers={action.headers || {}} onChange={(h) => updatePmAction(i, { headers: h })} />
                    </div>
                    <div>
                      <label className="text-[11px] text-gray-600 block mb-1">Body template <span className="text-gray-700">(supports {'{{variables}}'})</span></label>
                      <textarea className={`${inputClass} font-mono text-xs h-16`}
                        placeholder={'{"ref": "{{branch_name}}", "sha": "{{commit_hash}}"}'}
                        value={action.body_template || ''} onChange={(e) => updatePmAction(i, { body_template: e.target.value })} disabled={!isOwner}
                      />
                    </div>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <div>
                      <label className="text-[11px] text-gray-600 block mb-1">Shell command <span className="text-gray-700">(supports {'{{variables}}'}, values are shell-escaped)</span></label>
                      <textarea className={`${inputClass} font-mono text-xs h-16`}
                        placeholder={'cd /deploy && ./trigger-build.sh {{branch_name}} {{commit_hash}}'}
                        value={action.command || ''} onChange={(e) => updatePmAction(i, { command: e.target.value })} disabled={!isOwner}
                      />
                    </div>
                    <div>
                      <label className="text-[11px] text-gray-600 block mb-1">Timeout (seconds)</label>
                      <input className={`${inputClass} w-24 text-xs`} type="number"
                        value={action.timeout_seconds || 120} onChange={(e) => updatePmAction(i, { timeout_seconds: parseInt(e.target.value) || 120 })} disabled={!isOwner}
                      />
                    </div>
                  </div>
                )}
              </div>
            ))}

            {isOwner && (
              <div className="flex items-center gap-2">
                <button onClick={() => addPmAction('webhook')} className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-300 transition-colors">
                  <Plus className="w-3 h-3" /> Add Webhook
                </button>
                <span className="text-gray-700">|</span>
                <button onClick={() => addPmAction('script')} className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-300 transition-colors">
                  <Plus className="w-3 h-3" /> Add Script
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── Template Variables Reference ─────────────────────── */}
      {(config.enabled || pmConfig.enabled) && (
        <>
          <section className="space-y-2">
            <h4 className="text-xs text-gray-400 uppercase tracking-wider">Available Variables</h4>
            <p className="text-[11px] text-gray-600">
              Use <code className="text-gray-500">{'{{variable_name}}'}</code> in URLs, headers, body templates, and kubectl commands.
            </p>
            <div className="grid grid-cols-2 gap-1.5">
              {variables.map((v) => (
                <div key={v.key} className="flex items-center gap-2 px-2 py-1.5 bg-gray-900 border border-gray-800 rounded text-xs">
                  <code className="text-indigo-400 font-mono">{`{{${v.key}}}`}</code>
                  <span className="text-gray-600">{v.example}</span>
                </div>
              ))}
            </div>
          </section>

          {/* Save button */}
          {isOwner && (
            <div className="pt-2">
              <button onClick={save} disabled={saving}
                className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 rounded-lg text-sm font-medium text-white transition-colors"
              >
                {saving ? 'Saving...' : 'Save Pipeline Settings'}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
