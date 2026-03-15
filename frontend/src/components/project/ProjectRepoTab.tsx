import type { GitProviderConfig, RepoInfo } from '../../types'
import { inputClass, selectClass } from '../../styles/classes'
import RepoPicker from './RepoPicker'

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
  const handleRepoSelect = (repo: RepoInfo, providerId: string) => {
    setRepoUrl(repo.clone_url)
    setDefaultBranch(repo.default_branch)
    setGitProviderId(providerId)
  }

  return (
    <>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Repository URL</label>
          <RepoPicker
            value={repoUrl}
            onChange={setRepoUrl}
            onRepoSelect={handleRepoSelect}
            gitProviderList={gitProviderList}
            placeholder="Search repos or paste URL..."
          />
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
          <p className="text-[11px] text-gray-600 mt-1">Auto-set when picking a repo. Configure providers in Settings.</p>
        </div>
      )}
    </>
  )
}
