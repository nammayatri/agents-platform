import type { GitProviderConfig } from '../../types'
import { inputClass, selectClass } from '../../styles/classes'

interface ProjectRepoTabProps {
  repoUrl: string
  setRepoUrl: (v: string) => void
  defaultBranch: string
  setDefaultBranch: (v: string) => void
  gitProviderId: string
  setGitProviderId: (v: string) => void
  gitProviderList: GitProviderConfig[]
}

export default function ProjectRepoTab({
  repoUrl, setRepoUrl, defaultBranch, setDefaultBranch, gitProviderId, setGitProviderId, gitProviderList,
}: ProjectRepoTabProps) {
  return (
    <>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Repository URL</label>
          <input className={inputClass} placeholder="https://github.com/org/repo" value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Default Branch</label>
          <input className={inputClass} placeholder="main" value={defaultBranch} onChange={(e) => setDefaultBranch(e.target.value)} />
        </div>
      </div>
      {gitProviderList.length > 0 && (
        <div>
          <label className="block text-xs text-gray-500 mb-1">Git Provider</label>
          <select value={gitProviderId} onChange={(e) => setGitProviderId(e.target.value)} className={selectClass}>
            <option value="">None (public repos only)</option>
            {gitProviderList.map((g) => (
              <option key={g.id} value={g.id}>{g.display_name} ({g.provider_type})</option>
            ))}
          </select>
          <p className="text-[11px] text-gray-600 mt-1">For private repositories. Configure providers in Settings.</p>
        </div>
      )}
    </>
  )
}
