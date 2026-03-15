import type { ProviderConfig } from '../../types'
import { inputClass, selectClass } from '../../styles/classes'

interface ProjectGeneralTabProps {
  name: string
  setName: (v: string) => void
  description: string
  setDescription: (v: string) => void
  iconUrl: string
  setIconUrl: (v: string) => void
  aiProviderId: string
  setAiProviderId: (v: string) => void
  activeProviders: ProviderConfig[]
}

export default function ProjectGeneralTab({
  name, setName, description, setDescription, iconUrl, setIconUrl, aiProviderId, setAiProviderId, activeProviders,
}: ProjectGeneralTabProps) {
  return (
    <>
      <div>
        <label className="block text-xs text-gray-500 mb-1">Name</label>
        <input className={inputClass} placeholder="Project name" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div>
        <label className="block text-xs text-gray-500 mb-1">Description</label>
        <textarea className={`${inputClass} resize-none`} placeholder="What is this project about?" value={description} onChange={(e) => setDescription(e.target.value)} rows={3} />
      </div>
      <div>
        <label className="block text-xs text-gray-500 mb-1">Icon URL</label>
        <div className="flex items-center gap-3">
          {iconUrl && (
            <div className="w-8 h-8 rounded bg-gray-800/60 shrink-0 flex items-center justify-center overflow-hidden"><img src={iconUrl} alt="" className="w-6 h-6 object-contain" onError={(e) => { (e.target as HTMLImageElement).parentElement!.style.display = 'none' }} /></div>
          )}
          <input className={inputClass} placeholder="https://example.com/icon.png" value={iconUrl} onChange={(e) => setIconUrl(e.target.value)} />
        </div>
        <p className="text-[11px] text-gray-600 mt-1">Shown in sidebar next to project name.</p>
      </div>
      <div>
        <label className="block text-xs text-gray-500 mb-1">AI Provider</label>
        <select value={aiProviderId} onChange={(e) => setAiProviderId(e.target.value)} className={selectClass}>
          <option value="">Default (auto-resolve)</option>
          {activeProviders.map((p) => (
            <option key={p.id} value={p.id}>{p.display_name} -- {p.default_model}</option>
          ))}
        </select>
      </div>
    </>
  )
}
