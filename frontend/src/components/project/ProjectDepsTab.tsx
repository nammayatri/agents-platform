import type { ProjectDependency, GitProviderConfig } from '../../types'

interface ProjectDepsTabProps {
  dependencies: ProjectDependency[]
  setDependencies: (deps: ProjectDependency[]) => void
  gitProviderList: GitProviderConfig[]
}

export default function ProjectDepsTab({ dependencies, setDependencies, gitProviderList }: ProjectDepsTabProps) {
  const addDependency = () => setDependencies([...dependencies, { name: '', repo_url: '', description: '' }])
  const updateDependency = (i: number, field: keyof ProjectDependency, value: string) => {
    const updated = [...dependencies]
    updated[i] = { ...updated[i], [field]: value }
    setDependencies(updated)
  }
  const removeDependency = (i: number) => setDependencies(dependencies.filter((_, idx) => idx !== i))

  return (
    <>
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-gray-300">Context Dependencies</p>
          <p className="text-[11px] text-gray-600 mt-0.5">Libraries or repos this project depends on.</p>
        </div>
        <button onClick={addDependency} className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors">+ Add</button>
      </div>
      {dependencies.length === 0 && (
        <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">No dependencies configured</div>
      )}
      <div className="space-y-2">
        {dependencies.map((dep, i) => (
          <div key={i} className="p-3 bg-gray-900 border border-gray-800 rounded-lg">
            <div className="grid grid-cols-3 gap-2 mb-2">
              <input className="px-2 py-1.5 bg-gray-950 border border-gray-800 rounded text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors" placeholder="Name" value={dep.name} onChange={(e) => updateDependency(i, 'name', e.target.value)} />
              <input className="col-span-2 px-2 py-1.5 bg-gray-950 border border-gray-800 rounded text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors" placeholder="Repository URL (optional)" value={dep.repo_url || ''} onChange={(e) => updateDependency(i, 'repo_url', e.target.value)} />
            </div>
            <div className="flex gap-2">
              <input className="flex-1 px-2 py-1.5 bg-gray-950 border border-gray-800 rounded text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors" placeholder="Short description (optional)" value={dep.description || ''} onChange={(e) => updateDependency(i, 'description', e.target.value)} />
              {dep.repo_url && gitProviderList.length > 0 && (
                <select value={dep.git_provider_id || ''} onChange={(e) => updateDependency(i, 'git_provider_id', e.target.value)} className="px-2 py-1.5 bg-gray-950 border border-gray-800 rounded text-white text-xs focus:outline-none focus:border-indigo-500 transition-colors w-40">
                  <option value="">Git: inherit</option>
                  {gitProviderList.map((g) => (<option key={g.id} value={g.id}>{g.display_name}</option>))}
                </select>
              )}
              <button onClick={() => removeDependency(i)} className="px-2 py-1.5 text-gray-600 hover:text-red-400 transition-colors text-sm">Remove</button>
            </div>
          </div>
        ))}
      </div>
    </>
  )
}
