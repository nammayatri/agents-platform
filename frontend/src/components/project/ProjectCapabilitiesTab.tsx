import type { ProviderConfig, Skill, McpServer } from '../../types'

interface ProjectCapabilitiesTabProps {
  activeProviders: ProviderConfig[]
  skillList: Skill[]
  mcpList: McpServer[]
  disabledProviderIds: Set<string>
  setDisabledProviderIds: (ids: Set<string>) => void
  disabledSkillIds: Set<string>
  setDisabledSkillIds: (ids: Set<string>) => void
  disabledMcpIds: Set<string>
  setDisabledMcpIds: (ids: Set<string>) => void
}

function toggleDisabled(set: Set<string>, id: string): Set<string> {
  const next = new Set(set)
  if (next.has(id)) next.delete(id); else next.add(id)
  return next
}

export default function ProjectCapabilitiesTab({
  activeProviders, skillList, mcpList,
  disabledProviderIds, setDisabledProviderIds,
  disabledSkillIds, setDisabledSkillIds,
  disabledMcpIds, setDisabledMcpIds,
}: ProjectCapabilitiesTabProps) {
  return (
    <>
      <p className="text-xs text-gray-600">Toggle which providers, skills, and MCP servers are available for this project. All are enabled by default.</p>

      {activeProviders.length > 0 && (
        <div>
          <span className="text-[11px] text-gray-500 uppercase tracking-wider">Providers</span>
          <div className="mt-1.5 space-y-1">
            {activeProviders.map((p) => (
              <label key={p.id} className="flex items-center gap-2.5 px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg cursor-pointer hover:border-gray-700 transition-colors">
                <input type="checkbox" checked={!disabledProviderIds.has(p.id)} onChange={() => setDisabledProviderIds(toggleDisabled(disabledProviderIds, p.id))} className="rounded border-gray-600 bg-gray-800 text-indigo-500 focus:ring-indigo-500 focus:ring-offset-0" />
                <span className="text-sm text-white">{p.display_name}</span>
                <span className="text-xs text-gray-500">{p.default_model}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {skillList.length > 0 && (
        <div>
          <span className="text-[11px] text-gray-500 uppercase tracking-wider">Skills</span>
          <div className="mt-1.5 space-y-1">
            {skillList.map((s) => (
              <label key={s.id} className="flex items-center gap-2.5 px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg cursor-pointer hover:border-gray-700 transition-colors">
                <input type="checkbox" checked={!disabledSkillIds.has(s.id)} onChange={() => setDisabledSkillIds(toggleDisabled(disabledSkillIds, s.id))} className="rounded border-gray-600 bg-gray-800 text-indigo-500 focus:ring-indigo-500 focus:ring-offset-0" />
                <span className="text-sm text-white">{s.name}</span>
                <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500">{s.category}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {mcpList.length > 0 && (
        <div>
          <span className="text-[11px] text-gray-500 uppercase tracking-wider">MCP Servers</span>
          <div className="mt-1.5 space-y-1">
            {mcpList.map((m) => (
              <label key={m.id} className="flex items-center gap-2.5 px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg cursor-pointer hover:border-gray-700 transition-colors">
                <input type="checkbox" checked={!disabledMcpIds.has(m.id)} onChange={() => setDisabledMcpIds(toggleDisabled(disabledMcpIds, m.id))} className="rounded border-gray-600 bg-gray-800 text-indigo-500 focus:ring-indigo-500 focus:ring-offset-0" />
                <span className="text-sm text-white">{m.name}</span>
                <span className="text-xs text-gray-500 font-mono">{m.command}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {activeProviders.length === 0 && skillList.length === 0 && mcpList.length === 0 && (
        <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
          No capabilities configured. Add providers, skills, or MCP servers in Settings.
        </div>
      )}
    </>
  )
}
