import { useEffect, useState } from 'react'
import { mcpServers as mcpApi } from '../../services/api'
import type { McpServer, McpTool } from '../../types'
import { inputClass, btnPrimary, btnSecondary, btnDanger } from '../../styles/classes'

interface Props {
  isAdmin: boolean
}

export default function McpServersTab({ isAdmin: _isAdmin }: Props) {
  const [mcpList, setMcpList] = useState<McpServer[]>([])
  const [showMcpForm, setShowMcpForm] = useState(false)
  const [editingMcpId, setEditingMcpId] = useState<string | null>(null)
  const [mcpForm, setMcpForm] = useState({
    name: '',
    description: '',
    command: '',
    args: '',
    env_json: '',
    transport: 'stdio',
    url: '',
  })

  const [discoveringMcpId, setDiscoveringMcpId] = useState<string | null>(null)
  const [testingMcpId, setTestingMcpId] = useState<string | null>(null)
  const [mcpToolsResult, setMcpToolsResult] = useState<{ id: string; tools: McpTool[]; error?: string; transportUpdated?: string; urlUpdated?: string } | null>(null)
  const [mcpTestResult, setMcpTestResult] = useState<{ id: string; probes: { path?: string; status?: number; content_type?: string; supports_sse?: boolean; supports_streamable_http?: boolean; error?: string }[]; recommendation: { transport: string; url: string } | null } | null>(null)
  const [expandedMcpId, setExpandedMcpId] = useState<string | null>(null)

  useEffect(() => {
    mcpApi.list().then((m) => setMcpList(m as McpServer[])).catch(() => {})
  }, [])

  const handleDiscoverTools = async (id: string) => {
    setDiscoveringMcpId(id)
    setMcpToolsResult(null)
    setMcpTestResult(null)
    try {
      const result = await mcpApi.discoverTools(id)
      if (result.status === 'ok') {
        setMcpToolsResult({ id, tools: result.tools, transportUpdated: result.transport_updated, urlUpdated: result.url_updated })
        // Update the local list with discovered tools (and transport/url if auto-detected)
        setMcpList(mcpList.map((m) =>
          m.id === id ? {
            ...m,
            tools_json: result.tools,
            ...(result.transport_updated ? { transport: result.transport_updated as McpServer['transport'] } : {}),
            ...(result.url_updated ? { url: result.url_updated } : {}),
          } : m
        ))
      } else {
        setMcpToolsResult({ id, tools: [], error: result.detail || 'Discovery failed' })
      }
    } catch (err) {
      setMcpToolsResult({ id, tools: [], error: err instanceof Error ? err.message : 'Request failed' })
    } finally {
      setDiscoveringMcpId(null)
    }
  }

  const handleTestConnection = async (id: string) => {
    setTestingMcpId(id)
    setMcpTestResult(null)
    setMcpToolsResult(null)
    try {
      const result = await mcpApi.testConnection(id)
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      setMcpTestResult({ id, probes: result.probes as any, recommendation: result.recommendation })
    } catch {
      setMcpTestResult({ id, probes: [], recommendation: null })
    } finally {
      setTestingMcpId(null)
    }
  }

  const resetMcpForm = () => {
    setMcpForm({ name: '', description: '', command: '', args: '', env_json: '', transport: 'stdio', url: '' })
    setEditingMcpId(null)
    setShowMcpForm(false)
  }

  const startEditMcp = (m: McpServer) => {
    setMcpForm({
      name: m.name,
      description: m.description || '',
      command: m.command,
      args: m.args.join(' '),
      env_json: Object.keys(m.env_json || {}).length > 0 ? JSON.stringify(m.env_json, null, 2) : '',
      transport: m.transport,
      url: m.url || '',
    })
    setEditingMcpId(m.id)
    setShowMcpForm(true)
  }

  const handleSaveMcp = async () => {
    const data: Record<string, unknown> = {
      name: mcpForm.name,
      description: mcpForm.description || undefined,
      command: mcpForm.command,
      args: mcpForm.args.trim() ? mcpForm.args.trim().split(/\s+/) : [],
      transport: mcpForm.transport,
      url: mcpForm.url || undefined,
    }
    if (mcpForm.env_json.trim()) {
      try {
        data.env_json = JSON.parse(mcpForm.env_json)
      } catch {
        alert('Invalid JSON in environment variables')
        return
      }
    } else {
      data.env_json = {}
    }

    if (editingMcpId) {
      const updated = await mcpApi.update(editingMcpId, data) as McpServer
      setMcpList(mcpList.map((m) => (m.id === editingMcpId ? updated : m)))
    } else {
      const created = await mcpApi.create(data as never) as McpServer
      setMcpList([created, ...mcpList])
    }
    resetMcpForm()
  }

  const handleDeleteMcp = async (id: string) => {
    if (!confirm('Delete this MCP server?')) return
    await mcpApi.delete(id)
    setMcpList(mcpList.filter((m) => m.id !== id))
  }

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="text-sm font-medium text-gray-300 uppercase tracking-wider">MCP Servers</h2>
          <p className="text-xs text-gray-600 mt-1">
            Model Context Protocol servers that provide tools and resources to agents.
          </p>
        </div>
        {!showMcpForm && (
          <button
            onClick={() => {
              resetMcpForm()
              setShowMcpForm(true)
            }}
            className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            Add MCP server
          </button>
        )}
      </div>

      <div className="space-y-2 mb-3">
        {mcpList.map((m) => {
          const tools: McpTool[] = Array.isArray(m.tools_json)
            ? m.tools_json
            : typeof m.tools_json === 'string'
              ? (() => { try { return JSON.parse(m.tools_json) } catch { return [] } })()
              : []
          const isExpanded = expandedMcpId === m.id
          const discoveryError = mcpToolsResult?.id === m.id ? mcpToolsResult.error : undefined
          const transportUpdated = mcpToolsResult?.id === m.id ? mcpToolsResult.transportUpdated : undefined
          const testResult = mcpTestResult?.id === m.id ? mcpTestResult : null
          const isHttpTransport = m.transport === 'sse' || m.transport === 'streamable-http'

          return (
            <div key={m.id}>
              <div className="p-3 bg-gray-900 border border-gray-800 rounded-lg">
                <div className="flex items-center justify-between">
                  <div className="flex-1 min-w-0 mr-3">
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-white font-medium">{m.name}</span>
                      <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500 font-mono">
                        {m.transport}
                      </span>
                      {tools.length > 0 && (
                        <button
                          onClick={() => setExpandedMcpId(isExpanded ? null : m.id)}
                          className="px-1.5 py-0.5 bg-indigo-900/30 rounded text-[10px] text-indigo-400 hover:bg-indigo-900/50 transition-colors"
                        >
                          {tools.length} tool{tools.length !== 1 ? 's' : ''}
                        </button>
                      )}
                      {!m.is_active && (
                        <span className="px-1.5 py-0.5 bg-red-900/30 rounded text-[10px] text-red-400">
                          disabled
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-gray-500 mt-0.5 font-mono truncate">
                      {isHttpTransport ? m.url : `${m.command} ${m.args.join(' ')}`}
                    </div>
                  </div>
                  <div className="flex gap-2">
                    {isHttpTransport && (
                      <button
                        onClick={() => handleTestConnection(m.id)}
                        disabled={testingMcpId === m.id}
                        className="px-2 py-1 text-xs text-gray-400 hover:text-white transition-colors disabled:opacity-50"
                      >
                        {testingMcpId === m.id ? 'Testing...' : 'Test'}
                      </button>
                    )}
                    <button
                      onClick={() => handleDiscoverTools(m.id)}
                      disabled={discoveringMcpId === m.id}
                      className="px-2 py-1 text-xs text-gray-400 hover:text-white transition-colors disabled:opacity-50"
                    >
                      {discoveringMcpId === m.id ? 'Scanning...' : tools.length > 0 ? 'Refresh' : 'Discover tools'}
                    </button>
                    <button
                      onClick={() => startEditMcp(m)}
                      className="px-2 py-1 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
                    >
                      Edit
                    </button>
                    <button onClick={() => handleDeleteMcp(m.id)} className={btnDanger}>
                      Delete
                    </button>
                  </div>
                </div>

                {transportUpdated && (
                  <div className="mt-2 px-3 py-2 rounded text-xs bg-amber-900/30 text-amber-400 border border-amber-800/50">
                    Transport auto-updated to <span className="font-mono font-medium">{transportUpdated}</span>
                    {mcpToolsResult?.urlUpdated && (
                      <> at <span className="font-mono font-medium">{mcpToolsResult.urlUpdated}</span></>
                    )}
                  </div>
                )}

                {discoveryError && (
                  <div className="mt-2 px-3 py-2 rounded text-xs bg-red-900/30 text-red-400 border border-red-800/50">
                    <div className="flex items-center justify-between">
                      <span className="font-medium">Discovery failed</span>
                      <button onClick={() => setMcpToolsResult(null)} className="text-gray-500 hover:text-gray-300 ml-2">x</button>
                    </div>
                    <div className="mt-1 text-[11px] opacity-80 font-mono break-all">{discoveryError}</div>
                  </div>
                )}

                {testResult && (
                  <div className="mt-2 px-3 py-2 rounded text-xs border border-gray-700 bg-gray-950">
                    <div className="flex items-center justify-between mb-2">
                      <span className="font-medium text-gray-300">Connection Test Results</span>
                      <button onClick={() => setMcpTestResult(null)} className="text-gray-500 hover:text-gray-300">x</button>
                    </div>
                    <div className="space-y-1">
                      {testResult.probes.map((p, i) => (
                        <div key={i} className="flex items-center gap-2 font-mono text-[11px]">
                          <span className={`w-8 text-right ${p.status === 200 ? 'text-emerald-400' : p.status ? 'text-red-400' : 'text-gray-600'}`}>
                            {p.status || 'ERR'}
                          </span>
                          <span className="text-gray-400">{p.path}</span>
                          {p.content_type && <span className="text-gray-600 truncate">{p.content_type.split(';')[0]}</span>}
                          {p.supports_sse && <span className="text-emerald-400">SSE</span>}
                          {p.supports_streamable_http && <span className="text-emerald-400">Streamable-HTTP</span>}
                        </div>
                      ))}
                    </div>
                    {testResult.recommendation && (
                      <div className="mt-2 pt-2 border-t border-gray-800 text-emerald-400">
                        Recommended: <span className="font-mono font-medium">{testResult.recommendation.transport}</span> at <span className="font-mono font-medium">{testResult.recommendation.url}</span>
                      </div>
                    )}
                  </div>
                )}

                {isExpanded && tools.length > 0 && (
                  <div className="mt-2 border-t border-gray-800 pt-2">
                    <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1.5">Available Tools</div>
                    <div className="grid gap-1">
                      {tools.map((t) => (
                        <div key={t.name} className="flex items-start gap-2 px-2 py-1.5 bg-gray-950 rounded text-xs">
                          <span className="text-indigo-400 font-mono font-medium shrink-0">{t.name}</span>
                          {t.description && <span className="text-gray-500 truncate">{t.description}</span>}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
              {/* Inline edit form */}
              {editingMcpId === m.id && showMcpForm && (
                <div className="mt-1 p-4 bg-gray-900 border border-indigo-900/50 rounded-lg space-y-3">
                  <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">Edit MCP Server</div>
                  <div className="grid grid-cols-3 gap-3">
                    <div className="col-span-2">
                      <input className={inputClass} placeholder="Server name" value={mcpForm.name}
                        onChange={(e) => setMcpForm({ ...mcpForm, name: e.target.value })} />
                    </div>
                    <select value={mcpForm.transport} onChange={(e) => setMcpForm({ ...mcpForm, transport: e.target.value })} className={inputClass}>
                      <option value="stdio">stdio</option>
                      <option value="sse">SSE</option>
                      <option value="streamable-http">Streamable HTTP</option>
                    </select>
                  </div>
                  <input className={inputClass} placeholder="Description (optional)" value={mcpForm.description}
                    onChange={(e) => setMcpForm({ ...mcpForm, description: e.target.value })} />
                  <div className="grid grid-cols-3 gap-3">
                    <input className={inputClass} placeholder="Command" value={mcpForm.command}
                      onChange={(e) => setMcpForm({ ...mcpForm, command: e.target.value })} />
                    <div className="col-span-2">
                      <input className={inputClass} placeholder="Arguments (space-separated)" value={mcpForm.args}
                        onChange={(e) => setMcpForm({ ...mcpForm, args: e.target.value })} />
                    </div>
                  </div>
                  {(mcpForm.transport === 'sse' || mcpForm.transport === 'streamable-http') && (
                    <input className={inputClass} placeholder="Server URL" value={mcpForm.url}
                      onChange={(e) => setMcpForm({ ...mcpForm, url: e.target.value })} />
                  )}
                  <textarea className={`${inputClass} resize-none font-mono text-xs`}
                    placeholder='Environment variables as JSON' rows={3} value={mcpForm.env_json}
                    onChange={(e) => setMcpForm({ ...mcpForm, env_json: e.target.value })} />
                  <div className="flex gap-2">
                    <button onClick={handleSaveMcp} className={btnPrimary}>Update</button>
                    <button onClick={resetMcpForm} className={btnSecondary}>Cancel</button>
                  </div>
                </div>
              )}
            </div>
          )
        })}
        {mcpList.length === 0 && (
          <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
            No MCP servers configured. Add servers to give agents external tools.
          </div>
        )}
      </div>

      {/* Add new MCP server form */}
      {showMcpForm && !editingMcpId && (
        <div className="p-4 bg-gray-900 border border-gray-800 rounded-lg space-y-3">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">New MCP Server</div>
          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-2">
              <input className={inputClass} placeholder="Server name" value={mcpForm.name}
                onChange={(e) => setMcpForm({ ...mcpForm, name: e.target.value })} />
            </div>
            <select value={mcpForm.transport} onChange={(e) => setMcpForm({ ...mcpForm, transport: e.target.value })} className={inputClass}>
              <option value="stdio">stdio</option>
              <option value="sse">SSE</option>
              <option value="streamable-http">Streamable HTTP</option>
            </select>
          </div>
          <input className={inputClass} placeholder="Description (optional)" value={mcpForm.description}
            onChange={(e) => setMcpForm({ ...mcpForm, description: e.target.value })} />
          <div className="grid grid-cols-3 gap-3">
            <input className={inputClass} placeholder="Command (e.g. npx, uvx, python)" value={mcpForm.command}
              onChange={(e) => setMcpForm({ ...mcpForm, command: e.target.value })} />
            <div className="col-span-2">
              <input className={inputClass} placeholder="Arguments (space-separated, e.g. -y @mcp/server-github)" value={mcpForm.args}
                onChange={(e) => setMcpForm({ ...mcpForm, args: e.target.value })} />
            </div>
          </div>
          {(mcpForm.transport === 'sse' || mcpForm.transport === 'streamable-http') && (
            <input className={inputClass} placeholder="Server URL (e.g. http://localhost:3001/sse)" value={mcpForm.url}
              onChange={(e) => setMcpForm({ ...mcpForm, url: e.target.value })} />
          )}
          <textarea className={`${inputClass} resize-none font-mono text-xs`}
            placeholder='Environment variables as JSON (e.g. {"GITHUB_TOKEN": "ghp_..."})' rows={3}
            value={mcpForm.env_json} onChange={(e) => setMcpForm({ ...mcpForm, env_json: e.target.value })} />
          <div className="flex gap-2">
            <button onClick={handleSaveMcp} className={btnPrimary}>Save</button>
            <button onClick={resetMcpForm} className={btnSecondary}>Cancel</button>
          </div>
        </div>
      )}
    </section>
  )
}
