import { useState, useEffect } from 'react'
import { projects as projectsApi } from '../../services/api'
import { inputClass } from '../../styles/classes'
import type { DebugLogSource, DebugMcpHint, DebugContext } from '../../types'

interface ProjectDebugTabProps {
  projectId: string
  setError: (err: string) => void
}

const emptyLogSource = (): DebugLogSource => ({
  service_name: '',
  log_path: '',
  log_command: '',
  description: '',
})

const emptyMcpHint = (): DebugMcpHint => ({
  mcp_server_name: '',
  available_data: [],
  example_queries: [],
  notes: '',
})

export default function ProjectDebugTab({ projectId, setError }: ProjectDebugTabProps) {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [logSources, setLogSources] = useState<DebugLogSource[]>([])
  const [mcpHints, setMcpHints] = useState<DebugMcpHint[]>([])
  const [customInstructions, setCustomInstructions] = useState('')

  useEffect(() => {
    projectsApi.debugContext.get(projectId).then((ctx) => {
      setLogSources(ctx.log_sources || [])
      setMcpHints(ctx.mcp_hints || [])
      setCustomInstructions(ctx.custom_instructions || '')
    }).catch(() => {}).finally(() => setLoading(false))
  }, [projectId])

  const handleSave = async () => {
    setSaving(true)
    setError('')
    try {
      const data: DebugContext = {
        log_sources: logSources.filter((s) => s.service_name.trim()),
        mcp_hints: mcpHints.filter((h) => h.mcp_server_name.trim()),
        custom_instructions: customInstructions.trim() || undefined,
      }
      const result = await projectsApi.debugContext.update(projectId, data)
      setLogSources(result.log_sources || [])
      setMcpHints(result.mcp_hints || [])
      setCustomInstructions(result.custom_instructions || '')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save debug context')
    } finally {
      setSaving(false)
    }
  }

  const updateLogSource = (index: number, field: keyof DebugLogSource, value: string) => {
    const updated = [...logSources]
    updated[index] = { ...updated[index], [field]: value }
    setLogSources(updated)
  }

  const updateMcpHint = (index: number, field: keyof DebugMcpHint, value: string | string[]) => {
    const updated = [...mcpHints]
    updated[index] = { ...updated[index], [field]: value }
    setMcpHints(updated)
  }

  if (loading) {
    return <div className="animate-pulse space-y-4"><div className="h-4 bg-gray-800 rounded w-1/3" /><div className="h-20 bg-gray-800 rounded" /></div>
  }

  return (
    <>
      <div>
        <p className="text-sm text-gray-300">Debug Context</p>
        <p className="text-[11px] text-gray-600 mt-0.5">
          Configure log sources, MCP data hints, and custom instructions for the debugger agent.
          When a debugging task is created, this context is injected into the agent's prompt.
        </p>
      </div>

      {/* Log Sources */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-xs text-gray-500">Log Sources</label>
          <button
            onClick={() => setLogSources([...logSources, emptyLogSource()])}
            className="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            + Add Log Source
          </button>
        </div>
        {logSources.length === 0 ? (
          <p className="text-[11px] text-gray-700 italic">
            No log sources configured. The debugger agent will discover logs by exploring the codebase.
          </p>
        ) : (
          <div className="space-y-3">
            {logSources.map((src, i) => (
              <div key={i} className="px-3 py-2.5 bg-gray-900 border border-gray-800 rounded-lg space-y-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-gray-600 uppercase tracking-wider">Source {i + 1}</span>
                  <button
                    onClick={() => setLogSources(logSources.filter((_, idx) => idx !== i))}
                    className="text-[11px] text-gray-600 hover:text-red-400 transition-colors"
                  >
                    Remove
                  </button>
                </div>
                <input
                  className={inputClass}
                  placeholder="Service name (e.g. api, worker, nginx)"
                  value={src.service_name}
                  onChange={(e) => updateLogSource(i, 'service_name', e.target.value)}
                />
                <input
                  className={`${inputClass} font-mono text-xs`}
                  placeholder="Log path (e.g. /var/log/api/app.log)"
                  value={src.log_path || ''}
                  onChange={(e) => updateLogSource(i, 'log_path', e.target.value)}
                />
                <input
                  className={`${inputClass} font-mono text-xs`}
                  placeholder="Log command (e.g. kubectl logs -f deploy/api)"
                  value={src.log_command || ''}
                  onChange={(e) => updateLogSource(i, 'log_command', e.target.value)}
                />
                <input
                  className={inputClass}
                  placeholder="Description (e.g. JSON structured API logs)"
                  value={src.description || ''}
                  onChange={(e) => updateLogSource(i, 'description', e.target.value)}
                />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* MCP Data Hints */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-xs text-gray-500">MCP Data Hints</label>
          <button
            onClick={() => setMcpHints([...mcpHints, emptyMcpHint()])}
            className="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            + Add MCP Hint
          </button>
        </div>
        {mcpHints.length === 0 ? (
          <p className="text-[11px] text-gray-700 italic">
            No MCP data hints configured. Add hints to tell the debugger what data is available in your MCP servers.
          </p>
        ) : (
          <div className="space-y-3">
            {mcpHints.map((hint, i) => (
              <div key={i} className="px-3 py-2.5 bg-gray-900 border border-gray-800 rounded-lg space-y-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-gray-600 uppercase tracking-wider">MCP Hint {i + 1}</span>
                  <button
                    onClick={() => setMcpHints(mcpHints.filter((_, idx) => idx !== i))}
                    className="text-[11px] text-gray-600 hover:text-red-400 transition-colors"
                  >
                    Remove
                  </button>
                </div>
                <input
                  className={inputClass}
                  placeholder="MCP server name (e.g. clickhouse, vm-access)"
                  value={hint.mcp_server_name}
                  onChange={(e) => updateMcpHint(i, 'mcp_server_name', e.target.value)}
                />
                <div>
                  <label className="text-[11px] text-gray-600 mb-0.5 block">Available data (comma-separated)</label>
                  <input
                    className={inputClass}
                    placeholder="e.g. error_logs table, request_metrics, slow_queries"
                    value={(hint.available_data || []).join(', ')}
                    onChange={(e) => updateMcpHint(i, 'available_data', e.target.value.split(',').map((s) => s.trim()).filter(Boolean))}
                  />
                </div>
                <div>
                  <div className="flex items-center justify-between mb-0.5">
                    <label className="text-[11px] text-gray-600">Example queries</label>
                    <button
                      onClick={() => updateMcpHint(i, 'example_queries', [...(hint.example_queries || []), ''])}
                      className="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors"
                    >
                      + Add
                    </button>
                  </div>
                  {(hint.example_queries || []).map((q, qi) => (
                    <div key={qi} className="flex gap-1.5 mb-1">
                      <input
                        className={`${inputClass} flex-1 font-mono text-xs`}
                        placeholder="e.g. SELECT * FROM error_logs WHERE ts > now() - INTERVAL 1 HOUR"
                        value={q}
                        onChange={(e) => {
                          const queries = [...(hint.example_queries || [])]
                          queries[qi] = e.target.value
                          updateMcpHint(i, 'example_queries', queries)
                        }}
                      />
                      <button
                        onClick={() => {
                          const queries = (hint.example_queries || []).filter((_, idx) => idx !== qi)
                          updateMcpHint(i, 'example_queries', queries)
                        }}
                        className="px-2 text-gray-600 hover:text-red-400 transition-colors text-xs shrink-0"
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
                <input
                  className={inputClass}
                  placeholder="Notes (e.g. Use FORMAT JSON for ClickHouse queries)"
                  value={hint.notes || ''}
                  onChange={(e) => updateMcpHint(i, 'notes', e.target.value)}
                />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Custom Instructions */}
      <div>
        <label className="block text-xs text-gray-500 mb-1">Custom Debug Instructions</label>
        <textarea
          className={`${inputClass} min-h-[80px]`}
          placeholder="Free-form instructions for the debugger agent. E.g., 'Check Sentry for error reports', 'The API uses structured JSON logging', 'Common issues: Redis connection timeouts under load'"
          value={customInstructions}
          onChange={(e) => setCustomInstructions(e.target.value)}
          rows={4}
        />
        <p className="text-[11px] text-gray-600 mt-1">
          These instructions are injected into every debugger agent's system prompt for this project.
        </p>
      </div>

      <div className="pt-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
        >
          {saving ? 'Saving...' : 'Save Debug Context'}
        </button>
      </div>
    </>
  )
}
