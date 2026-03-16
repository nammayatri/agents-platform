import { useState, useEffect, useRef, useCallback } from 'react'
import { agents as agentsApi } from '../services/api'
import type { AgentConfig, AgentChatMessage, DefaultAgentInfo, AvailableTool } from '../types'

const roleColorMap: Record<string, { bg: string; text: string }> = {
  coder: { bg: 'bg-indigo-500/10', text: 'text-indigo-400' },
  tester: { bg: 'bg-emerald-500/10', text: 'text-emerald-400' },
  reviewer: { bg: 'bg-amber-500/10', text: 'text-amber-400' },
  pr_creator: { bg: 'bg-purple-500/10', text: 'text-purple-400' },
  report_writer: { bg: 'bg-cyan-500/10', text: 'text-cyan-400' },
  merge_agent: { bg: 'bg-emerald-500/10', text: 'text-emerald-400' },
}
const defaultColor = { bg: 'bg-gray-800', text: 'text-gray-400' }

type Tab = 'agents' | 'builder'

export default function AgentsPage() {
  const [tab, setTab] = useState<Tab>('agents')
  const [defaults, setDefaults] = useState<DefaultAgentInfo[]>([])
  const [overrides, setOverrides] = useState<Record<string, AgentConfig>>({})
  const [customAgents, setCustomAgents] = useState<AgentConfig[]>([])
  const [availableTools, setAvailableTools] = useState<AvailableTool[]>([])
  const [expandedRole, setExpandedRole] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [chatMessages, setChatMessages] = useState<AgentChatMessage[]>([])
  const [chatInput, setChatInput] = useState('')
  const [sending, setSending] = useState(false)
  const [loading, setLoading] = useState(true)
  const chatEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const loadAgents = useCallback(async () => {
    try {
      const [data, toolsData] = await Promise.all([
        agentsApi.list(),
        agentsApi.listTools(),
      ])
      setDefaults(data.defaults || [])
      setOverrides(data.overrides || {})
      setCustomAgents(data.custom || [])
      setAvailableTools([...(toolsData.builtin || []), ...(toolsData.mcp || [])])
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadAgents()
  }, [loadAgents])

  useEffect(() => {
    if (tab === 'builder') loadChat()
  }, [tab])

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMessages])

  async function loadChat() {
    try {
      const msgs = await agentsApi.chatHistory()
      setChatMessages(msgs as AgentChatMessage[])
    } catch {
      // ignore
    }
  }

  async function handleSend() {
    const content = chatInput.trim()
    if (!content || sending) return

    setChatInput('')
    setSending(true)

    const tempMsg: AgentChatMessage = {
      id: `temp-${Date.now()}`,
      user_id: '',
      role: 'user',
      content,
      created_at: new Date().toISOString(),
    }
    setChatMessages((prev) => [...prev, tempMsg])

    try {
      const resp = await agentsApi.chatSend(content)
      const userMsg = resp.user_message as AgentChatMessage
      const assistantMsg = resp.assistant_message as AgentChatMessage

      setChatMessages((prev) => {
        const without = prev.filter((m) => m.id !== tempMsg.id)
        return [...without, userMsg, assistantMsg]
      })

      if (assistantMsg.metadata_json?.action?.startsWith('agent_')) {
        loadAgents()
      }
    } catch (err) {
      setChatMessages((prev) => {
        const without = prev.filter((m) => m.id !== tempMsg.id)
        return [
          ...without,
          tempMsg,
          {
            id: `err-${Date.now()}`,
            user_id: '',
            role: 'system',
            content: `Error: ${err instanceof Error ? err.message : 'Failed to send'}`,
            created_at: new Date().toISOString(),
          },
        ]
      })
    } finally {
      setSending(false)
      inputRef.current?.focus()
    }
  }

  async function handleClearChat() {
    try {
      await agentsApi.chatClear()
      setChatMessages([])
    } catch {
      // ignore
    }
  }

  async function handleDeleteAgent(id: string) {
    try {
      await agentsApi.delete(id)
      setCustomAgents((prev) => prev.filter((a) => a.id !== id))
      // Also remove from overrides if it's an override
      setOverrides((prev) => {
        const next = { ...prev }
        for (const [role, cfg] of Object.entries(next)) {
          if (cfg.id === id) delete next[role]
        }
        return next
      })
      if (editingId === id) setEditingId(null)
    } catch {
      // ignore
    }
  }

  async function handleToggleAgent(agent: AgentConfig) {
    try {
      const updated = await agentsApi.update(agent.id, { is_active: !agent.is_active })
      setCustomAgents((prev) =>
        prev.map((a) => (a.id === agent.id ? updated : a))
      )
    } catch {
      // ignore
    }
  }

  async function handleSaveAgent(id: string, data: Record<string, unknown>) {
    try {
      const updated = await agentsApi.update(id, data)
      // Update in custom agents
      setCustomAgents((prev) =>
        prev.map((a) => (a.id === id ? updated : a))
      )
      // Update in overrides if applicable
      setOverrides((prev) => {
        const next = { ...prev }
        for (const [role, cfg] of Object.entries(next)) {
          if (cfg.id === id) next[role] = updated
        }
        return next
      })
      setEditingId(null)
    } catch {
      // ignore
    }
  }

  async function handleCreateOverride(role: string) {
    try {
      const created = await agentsApi.createOverride(role, {})
      setOverrides((prev) => ({ ...prev, [role]: created }))
      setEditingId(created.id)
    } catch {
      // ignore
    }
  }

  async function handleResetOverride(role: string) {
    const override = overrides[role]
    if (!override) return
    try {
      await agentsApi.delete(override.id)
      setOverrides((prev) => {
        const next = { ...prev }
        delete next[role]
        return next
      })
      setEditingId(null)
    } catch {
      // ignore
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="flex h-full">
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Header with tabs */}
        <div className="border-b border-gray-900 px-4 md:px-6 pt-5 pb-0">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h1 className="text-xl font-semibold text-white">Agents</h1>
              <p className="text-sm text-gray-500 mt-0.5">
                Manage default and custom AI agents for your projects.
              </p>
            </div>
          </div>
          <div className="flex gap-4">
            <button
              onClick={() => setTab('agents')}
              className={`pb-2.5 px-1 text-sm font-medium border-b-2 transition-colors ${
                tab === 'agents'
                  ? 'text-white border-indigo-500'
                  : 'text-gray-500 border-transparent hover:text-gray-300'
              }`}
            >
              All Agents
            </button>
            <button
              onClick={() => setTab('builder')}
              className={`pb-2.5 px-1 text-sm font-medium border-b-2 transition-colors ${
                tab === 'builder'
                  ? 'text-white border-indigo-500'
                  : 'text-gray-500 border-transparent hover:text-gray-300'
              }`}
            >
              Agent Builder
            </button>
          </div>
        </div>

        {/* Tab content */}
        {tab === 'agents' ? (
          <AgentsListTab
            defaults={defaults}
            overrides={overrides}
            customAgents={customAgents}
            availableTools={availableTools}
            loading={loading}
            expandedRole={expandedRole}
            setExpandedRole={setExpandedRole}
            editingId={editingId}
            setEditingId={setEditingId}
            onDelete={handleDeleteAgent}
            onToggle={handleToggleAgent}
            onSave={handleSaveAgent}
            onCreateOverride={handleCreateOverride}
            onResetOverride={handleResetOverride}
          />
        ) : (
          <BuilderTab
            chatMessages={chatMessages}
            chatInput={chatInput}
            setChatInput={setChatInput}
            sending={sending}
            onSend={handleSend}
            onClear={handleClearChat}
            onKeyDown={handleKeyDown}
            chatEndRef={chatEndRef}
            inputRef={inputRef}
          />
        )}
      </div>
    </div>
  )
}

/* ─── Tool Chip ─────────────────────────────────────────────────── */

function ToolChip({
  name,
  active,
  onClick,
}: {
  name: string
  active: boolean
  onClick?: () => void
}) {
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`px-2 py-0.5 rounded text-[10px] transition-colors ${
          active
            ? 'bg-indigo-500/10 text-indigo-400 hover:bg-indigo-500/20'
            : 'bg-gray-800/50 text-gray-600 hover:bg-gray-800 hover:text-gray-500'
        }`}
      >
        {name}
      </button>
    )
  }
  return (
    <span
      className={`px-2 py-0.5 rounded text-[10px] ${
        active ? 'bg-indigo-500/10 text-indigo-400' : 'bg-gray-800/50 text-gray-600'
      }`}
    >
      {name}
    </span>
  )
}

/* ─── Agent Edit Form ───────────────────────────────────────────── */

function AgentEditForm({
  initialPrompt,
  initialTools,
  initialModel,
  availableTools,
  onSave,
  onCancel,
}: {
  initialPrompt: string
  initialTools: string[]
  initialModel: string
  availableTools: AvailableTool[]
  onSave: (data: { system_prompt: string; tools_enabled: string[]; model_preference: string | null }) => void
  onCancel: () => void
}) {
  const [prompt, setPrompt] = useState(initialPrompt)
  const [tools, setTools] = useState<Set<string>>(new Set(initialTools))
  const [model, setModel] = useState(initialModel)

  const builtinTools = availableTools.filter((t) => t.category === 'builtin')
  const mcpTools = availableTools.filter((t) => t.category === 'mcp')

  function toggleTool(name: string) {
    setTools((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  return (
    <div className="space-y-4">
      <div>
        <label className="text-[10px] text-gray-600 uppercase tracking-wider block mb-1">
          System Prompt
        </label>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={5}
          className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-xs text-gray-300 font-mono focus:outline-none focus:border-gray-700 resize-y"
        />
      </div>

      <div>
        <label className="text-[10px] text-gray-600 uppercase tracking-wider block mb-1.5">
          Workspace Tools
        </label>
        <div className="flex flex-wrap gap-1.5">
          {builtinTools.map((t) => (
            <ToolChip
              key={t.name}
              name={t.name}
              active={tools.has(t.name)}
              onClick={() => toggleTool(t.name)}
            />
          ))}
        </div>
      </div>

      {mcpTools.length > 0 && (
        <div>
          <label className="text-[10px] text-gray-600 uppercase tracking-wider block mb-1.5">
            MCP Tools
          </label>
          <div className="flex flex-wrap gap-1.5">
            {mcpTools.map((t) => (
              <ToolChip
                key={t.name}
                name={`${t.server_name}/${t.name}`}
                active={tools.has(t.name)}
                onClick={() => toggleTool(t.name)}
              />
            ))}
          </div>
        </div>
      )}

      <div>
        <label className="text-[10px] text-gray-600 uppercase tracking-wider block mb-1">
          Model Preference
        </label>
        <input
          type="text"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="Default (from provider)"
          className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-1.5 text-xs text-gray-300 focus:outline-none focus:border-gray-700"
        />
      </div>

      <div className="flex items-center gap-2 pt-1">
        <button
          onClick={() =>
            onSave({
              system_prompt: prompt,
              tools_enabled: Array.from(tools),
              model_preference: model || null,
            })
          }
          className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white text-xs rounded-lg transition-colors"
        >
          Save
        </button>
        <button
          onClick={onCancel}
          className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-400 text-xs rounded-lg transition-colors"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

/* ─── Agents List Tab ────────────────────────────────────────────── */

function AgentsListTab({
  defaults,
  overrides,
  customAgents,
  availableTools,
  loading,
  expandedRole,
  setExpandedRole,
  editingId,
  setEditingId,
  onDelete,
  onToggle,
  onSave,
  onCreateOverride,
  onResetOverride,
}: {
  defaults: DefaultAgentInfo[]
  overrides: Record<string, AgentConfig>
  customAgents: AgentConfig[]
  availableTools: AvailableTool[]
  loading: boolean
  expandedRole: string | null
  setExpandedRole: (r: string | null) => void
  editingId: string | null
  setEditingId: (id: string | null) => void
  onDelete: (id: string) => void
  onToggle: (a: AgentConfig) => void
  onSave: (id: string, data: Record<string, unknown>) => void
  onCreateOverride: (role: string) => void
  onResetOverride: (role: string) => void
}) {
  return (
    <div className="flex-1 overflow-y-auto p-6">
      <div className="max-w-2xl mx-auto space-y-8">
        {/* Default Agent Team */}
        <section>
          <h2 className="text-sm font-medium text-gray-300 mb-1 uppercase tracking-wider">
            Default Agent Team
          </h2>
          <p className="text-xs text-gray-600 mb-4">
            These specialist agents collaborate to complete tasks. Click to see details or customize.
          </p>

          {loading ? (
            <div className="py-8 text-center text-sm text-gray-600">Loading...</div>
          ) : (
            <div className="space-y-2">
              {defaults.map((agent) => {
                const c = roleColorMap[agent.role] || defaultColor
                const isExpanded = expandedRole === agent.role
                const override = overrides[agent.role]
                const hasOverride = !!override
                const isEditing = hasOverride && editingId === override.id

                // Display values: override takes precedence
                const displayPrompt = hasOverride ? override.system_prompt : agent.system_prompt
                const displayTools = hasOverride ? override.tools_enabled : agent.default_tools
                const displayModel = hasOverride
                  ? override.model_preference || ''
                  : agent.default_model || ''

                return (
                  <div
                    key={agent.role}
                    className={`bg-gray-900 border border-gray-800 rounded-lg overflow-hidden transition-colors ${
                      isExpanded ? 'border-gray-700' : ''
                    }`}
                  >
                    <button
                      onClick={() => setExpandedRole(isExpanded ? null : agent.role)}
                      className="w-full flex items-center gap-3 px-4 py-3 text-left"
                    >
                      <div
                        className={`w-8 h-8 rounded-full ${c.bg} flex items-center justify-center text-xs font-semibold ${c.text} shrink-0`}
                      >
                        {agent.name.charAt(0)}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <p className="text-sm text-white font-medium">{agent.name}</p>
                          {hasOverride && (
                            <span className="px-1.5 py-0.5 rounded text-[9px] bg-amber-500/10 text-amber-400">
                              customized
                            </span>
                          )}
                        </div>
                        <p className="text-xs text-gray-500 truncate">{agent.description}</p>
                      </div>
                      <span
                        className={`px-2 py-0.5 rounded text-[10px] font-mono ${c.bg} ${c.text}`}
                      >
                        {agent.role}
                      </span>
                      <svg
                        className={`w-4 h-4 text-gray-600 transition-transform ${
                          isExpanded ? 'rotate-180' : ''
                        }`}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth={1.5}
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M19.5 8.25l-7.5 7.5-7.5-7.5"
                        />
                      </svg>
                    </button>

                    {isExpanded && (
                      <div className="px-4 pb-4 pt-0">
                        <div className="border-t border-gray-800 pt-3">
                          {isEditing ? (
                            <AgentEditForm
                              initialPrompt={displayPrompt}
                              initialTools={displayTools}
                              initialModel={displayModel}
                              availableTools={availableTools}
                              onSave={(data) => onSave(override.id, data)}
                              onCancel={() => setEditingId(null)}
                            />
                          ) : (
                            <div className="space-y-3">
                              <div>
                                <span className="text-[10px] text-gray-600 uppercase tracking-wider">
                                  System Prompt
                                </span>
                                <pre className="mt-1 text-xs text-gray-400 bg-gray-950 rounded p-3 max-h-40 overflow-y-auto whitespace-pre-wrap font-mono">
                                  {displayPrompt}
                                </pre>
                              </div>

                              <div>
                                <span className="text-[10px] text-gray-600 uppercase tracking-wider">
                                  Tools
                                </span>
                                <div className="flex flex-wrap gap-1.5 mt-1">
                                  {displayTools.map((t) => (
                                    <ToolChip key={t} name={t} active />
                                  ))}
                                </div>
                              </div>

                              {displayModel && (
                                <div>
                                  <span className="text-[10px] text-gray-600 uppercase tracking-wider">
                                    Model
                                  </span>
                                  <p className="text-xs text-gray-400 mt-0.5">{displayModel}</p>
                                </div>
                              )}

                              <div className="flex items-center gap-2 pt-1">
                                {hasOverride ? (
                                  <>
                                    <button
                                      onClick={() => setEditingId(override.id)}
                                      className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs rounded-lg transition-colors"
                                    >
                                      Edit
                                    </button>
                                    <button
                                      onClick={() => onResetOverride(agent.role)}
                                      className="text-xs text-gray-600 hover:text-gray-400 transition-colors"
                                    >
                                      Reset to default
                                    </button>
                                  </>
                                ) : (
                                  <button
                                    onClick={() => onCreateOverride(agent.role)}
                                    className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs rounded-lg transition-colors"
                                  >
                                    Customize
                                  </button>
                                )}
                              </div>
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </section>

        {/* Custom Agents */}
        <section>
          <h2 className="text-sm font-medium text-gray-300 mb-1 uppercase tracking-wider">
            Custom Agents
          </h2>
          <p className="text-xs text-gray-600 mb-4">
            Agents you've created with custom roles and system prompts. Use the Agent Builder tab
            to create new ones via chat.
          </p>

          {loading ? (
            <div className="py-8 text-center text-sm text-gray-600">Loading...</div>
          ) : customAgents.length === 0 ? (
            <div className="py-8 text-center border border-dashed border-gray-800 rounded-lg">
              <svg
                className="w-8 h-8 mx-auto text-gray-700 mb-3"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M18 18.72a9.094 9.094 0 003.741-.479 3 3 0 00-4.682-2.72m.94 3.198l.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0112 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 016 18.719m12 0a5.971 5.971 0 00-.941-3.197m0 0A5.995 5.995 0 0012 12.75a5.995 5.995 0 00-5.058 2.772m0 0a3 3 0 00-4.681 2.72 8.986 8.986 0 003.74.477m.94-3.197a5.971 5.971 0 00-.94 3.197M15 6.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z"
                />
              </svg>
              <p className="text-sm text-gray-500 mb-1">No custom agents yet</p>
              <p className="text-xs text-gray-600 max-w-xs mx-auto">
                Switch to the Agent Builder tab and describe the agent you want to create.
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {customAgents.map((agent) => {
                const isExpanded = editingId === agent.id
                const isEditing = isExpanded
                return (
                  <div
                    key={agent.id}
                    className={`bg-gray-900 border rounded-lg overflow-hidden transition-colors ${
                      isExpanded ? 'border-gray-700' : 'border-gray-800'
                    }`}
                  >
                    <div className="flex items-center gap-3 px-4 py-3">
                      <div
                        className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-semibold shrink-0 ${
                          agent.is_active
                            ? 'bg-indigo-500/10 text-indigo-400'
                            : 'bg-gray-800 text-gray-600'
                        }`}
                      >
                        {agent.name.charAt(0)}
                      </div>
                      <div className="flex-1 min-w-0">
                        <p
                          className={`text-sm font-medium ${
                            agent.is_active ? 'text-white' : 'text-gray-500'
                          }`}
                        >
                          {agent.name}
                        </p>
                        <p className="text-xs text-gray-500 truncate">
                          {agent.description || agent.role}
                        </p>
                      </div>
                      <span className="px-2 py-0.5 rounded text-[10px] font-mono bg-gray-800 text-gray-400">
                        {agent.role}
                      </span>
                      <button
                        onClick={() => onToggle(agent)}
                        className={`px-2 py-0.5 rounded text-[10px] transition-colors ${
                          agent.is_active
                            ? 'bg-emerald-500/10 text-emerald-400'
                            : 'bg-gray-800 text-gray-500'
                        }`}
                      >
                        {agent.is_active ? 'Active' : 'Inactive'}
                      </button>
                      <button
                        onClick={() => setEditingId(isExpanded ? null : agent.id)}
                        className="text-gray-600 hover:text-gray-400 transition-colors"
                      >
                        <svg
                          className={`w-4 h-4 transition-transform ${
                            isExpanded ? 'rotate-180' : ''
                          }`}
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                          strokeWidth={1.5}
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            d="M19.5 8.25l-7.5 7.5-7.5-7.5"
                          />
                        </svg>
                      </button>
                    </div>

                    {isExpanded && (
                      <div className="px-4 pb-4 border-t border-gray-800">
                        <div className="pt-3">
                          {isEditing ? (
                            <div className="space-y-3">
                              <AgentEditForm
                                initialPrompt={agent.system_prompt}
                                initialTools={agent.tools_enabled}
                                initialModel={agent.model_preference || ''}
                                availableTools={availableTools}
                                onSave={(data) => onSave(agent.id, data)}
                                onCancel={() => setEditingId(null)}
                              />
                              <div className="flex items-center pt-1 border-t border-gray-800">
                                <button
                                  onClick={() => onDelete(agent.id)}
                                  className="text-xs text-red-400/60 hover:text-red-400 transition-colors pt-2"
                                >
                                  Delete agent
                                </button>
                              </div>
                            </div>
                          ) : null}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

/* ─── Agent Builder Tab (Chat) ───────────────────────────────────── */

function BuilderTab({
  chatMessages,
  chatInput,
  setChatInput,
  sending,
  onSend,
  onClear,
  onKeyDown,
  chatEndRef,
  inputRef,
}: {
  chatMessages: AgentChatMessage[]
  chatInput: string
  setChatInput: (v: string) => void
  sending: boolean
  onSend: () => void
  onClear: () => void
  onKeyDown: (e: React.KeyboardEvent) => void
  chatEndRef: React.RefObject<HTMLDivElement | null>
  inputRef: React.RefObject<HTMLTextAreaElement | null>
}) {
  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Chat messages */}
      <div className="flex-1 overflow-y-auto">
        {chatMessages.length === 0 ? (
          <div className="h-full flex items-center justify-center">
            <div className="text-center max-w-sm">
              <div className="w-12 h-12 rounded-full bg-indigo-500/10 flex items-center justify-center mx-auto mb-4">
                <svg
                  className="w-6 h-6 text-indigo-400"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={1.5}
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z"
                  />
                </svg>
              </div>
              <h3 className="text-sm font-medium text-white mb-2">Agent Builder</h3>
              <p className="text-xs text-gray-500 leading-relaxed mb-4">
                Describe the kind of agent you want to create and I'll build it for you. I can set
                up specialized roles, system prompts, and tool access.
              </p>
              <div className="space-y-2 text-left">
                {[
                  'Create a frontend developer agent that specializes in React and TypeScript',
                  'Build a DevOps agent for CI/CD pipeline management',
                  'Make a security auditor that reviews code for vulnerabilities',
                ].map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => {
                      setChatInput(suggestion)
                      inputRef.current?.focus()
                    }}
                    className="w-full text-left px-3 py-2 rounded-lg border border-gray-800 hover:border-gray-700 text-xs text-gray-400 hover:text-gray-300 transition-colors"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <div className="max-w-2xl mx-auto px-6 py-4 space-y-4">
            {chatMessages.map((msg) => (
              <ChatBubble key={msg.id} message={msg} />
            ))}
            {sending && (
              <div className="flex gap-3">
                <div className="w-7 h-7 rounded-full bg-indigo-500/10 flex items-center justify-center shrink-0">
                  <svg
                    className="w-3.5 h-3.5 text-indigo-400 animate-pulse"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={1.5}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"
                    />
                  </svg>
                </div>
                <div className="bg-gray-900 border border-gray-800 rounded-lg px-3 py-2">
                  <div className="flex gap-1">
                    <span className="w-1.5 h-1.5 bg-gray-600 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                    <span className="w-1.5 h-1.5 bg-gray-600 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                    <span className="w-1.5 h-1.5 bg-gray-600 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                </div>
              </div>
            )}
            <div ref={chatEndRef} />
          </div>
        )}
      </div>

      {/* Input area */}
      <div className="border-t border-gray-900 px-4 md:px-6 py-3">
        <div className="max-w-2xl mx-auto">
          <div className="flex items-end gap-2">
            <div className="flex-1 relative">
              <textarea
                ref={inputRef}
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Describe the agent you want to create..."
                rows={1}
                className="w-full bg-gray-900 border border-gray-800 rounded-lg px-3 py-2.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-700 resize-none"
                style={{ minHeight: '40px', maxHeight: '120px' }}
                onInput={(e) => {
                  const t = e.currentTarget
                  t.style.height = 'auto'
                  t.style.height = Math.min(t.scrollHeight, 120) + 'px'
                }}
              />
            </div>
            <button
              onClick={onSend}
              disabled={!chatInput.trim() || sending}
              className="px-3 py-2.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-800 disabled:text-gray-600 text-white text-sm rounded-lg transition-colors shrink-0"
            >
              <svg
                className="w-4 h-4"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5"
                />
              </svg>
            </button>
          </div>
          <div className="flex items-center justify-between mt-2">
            <p className="text-[10px] text-gray-700">
              Shift+Enter for new line
            </p>
            {chatMessages.length > 0 && (
              <button
                onClick={onClear}
                className="text-[10px] text-gray-700 hover:text-gray-500 transition-colors"
              >
                Clear chat
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/* ─── Chat Bubble ────────────────────────────────────────────────── */

function ChatBubble({ message }: { message: AgentChatMessage }) {
  if (message.role === 'user') {
    return (
      <div className="flex gap-3 justify-end">
        <div className="bg-indigo-600/20 border border-indigo-500/20 rounded-lg px-3 py-2 max-w-[80%]">
          <p className="text-sm text-gray-200 whitespace-pre-wrap">{message.content}</p>
        </div>
      </div>
    )
  }

  if (message.role === 'system') {
    return (
      <div className="flex justify-center">
        <p className="text-xs text-red-400/70 bg-red-500/5 rounded px-3 py-1.5">
          {message.content}
        </p>
      </div>
    )
  }

  // Assistant message
  const meta = message.metadata_json
  return (
    <div className="flex gap-3">
      <div className="w-7 h-7 rounded-full bg-indigo-500/10 flex items-center justify-center shrink-0 mt-0.5">
        <svg
          className="w-3.5 h-3.5 text-indigo-400"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={1.5}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"
          />
        </svg>
      </div>
      <div className="flex-1 min-w-0 space-y-2">
        {meta?.action === 'agent_created' && (
          <div className="flex items-center gap-2 px-3 py-1.5 bg-emerald-500/5 border border-emerald-500/20 rounded-lg">
            <svg
              className="w-3.5 h-3.5 text-emerald-400 shrink-0"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
            </svg>
            <span className="text-xs text-emerald-400">
              Agent created: {meta.agent_name}
            </span>
          </div>
        )}
        {meta?.action === 'agent_updated' && (
          <div className="flex items-center gap-2 px-3 py-1.5 bg-amber-500/5 border border-amber-500/20 rounded-lg">
            <svg
              className="w-3.5 h-3.5 text-amber-400 shrink-0"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182"
              />
            </svg>
            <span className="text-xs text-amber-400">Agent updated</span>
          </div>
        )}
        <div className="bg-gray-900 border border-gray-800 rounded-lg px-3 py-2">
          <p className="text-sm text-gray-300 whitespace-pre-wrap">{message.content}</p>
        </div>
      </div>
    </div>
  )
}
