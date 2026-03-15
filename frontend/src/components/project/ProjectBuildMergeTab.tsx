import { useState } from 'react'
import { projects as projectsApi } from '../../services/api'
import { inputClass, selectClass } from '../../styles/classes'

interface ProjectBuildMergeTabProps {
  projectId: string
  buildCommands: string[]
  setBuildCommands: (cmds: string[]) => void
  mergeMethod: 'merge' | 'squash' | 'rebase'
  setMergeMethod: (method: 'merge' | 'squash' | 'rebase') => void
  setError: (err: string) => void
}

export default function ProjectBuildMergeTab({
  projectId, buildCommands, setBuildCommands, mergeMethod, setMergeMethod, setError,
}: ProjectBuildMergeTabProps) {
  const [saving, setSaving] = useState(false)

  return (
    <>
      <div>
        <p className="text-sm text-gray-300">Build & Merge Configuration</p>
        <p className="text-[11px] text-gray-600 mt-0.5">
          Configure how PRs are merged and what build commands run after merge.
        </p>
      </div>

      <div>
        <label className="block text-xs text-gray-500 mb-1">Merge Method</label>
        <select
          value={mergeMethod}
          onChange={(e) => setMergeMethod(e.target.value as 'merge' | 'squash' | 'rebase')}
          className={selectClass}
        >
          <option value="squash">Squash and Merge</option>
          <option value="merge">Merge Commit</option>
          <option value="rebase">Rebase and Merge</option>
        </select>
        <p className="text-[11px] text-gray-600 mt-1">
          Used by the merge agent when merging approved PRs.
        </p>
      </div>

      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-xs text-gray-500">Post-Merge Build Commands</label>
          <button
            onClick={() => setBuildCommands([...buildCommands, ''])}
            className="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            + Add
          </button>
        </div>
        {buildCommands.length === 0 ? (
          <p className="text-[11px] text-gray-700 italic">No build commands configured. The merge agent will skip post-merge builds.</p>
        ) : (
          <div className="space-y-1">
            {buildCommands.map((cmd, i) => (
              <div key={i} className="flex gap-1.5">
                <input
                  className={`${inputClass} flex-1 font-mono text-xs`}
                  placeholder="e.g. npm run build"
                  value={cmd}
                  onChange={(e) => {
                    const updated = [...buildCommands]
                    updated[i] = e.target.value
                    setBuildCommands(updated)
                  }}
                />
                <button
                  onClick={() => setBuildCommands(buildCommands.filter((_, idx) => idx !== i))}
                  className="px-2 text-gray-600 hover:text-red-400 transition-colors text-xs shrink-0"
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
        )}
        <p className="text-[11px] text-gray-600 mt-1">
          Commands run in the repo directory after a PR is merged. If any command fails, a message is posted to the task chat.
        </p>
      </div>

      <div className="pt-2">
        <button
          onClick={async () => {
            if (!projectId) return
            setSaving(true)
            setError('')
            try {
              const cleanedCmds = buildCommands.filter((c) => c.trim())
              await projectsApi.update(projectId, {
                settings_json: {
                  build_commands: cleanedCmds,
                  merge_method: mergeMethod,
                },
              })
              setBuildCommands(cleanedCmds)
            } catch (e) {
              setError(e instanceof Error ? e.message : 'Failed to save build settings')
            } finally {
              setSaving(false)
            }
          }}
          disabled={saving}
          className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
        >
          {saving ? 'Saving...' : 'Save Build Settings'}
        </button>
      </div>
    </>
  )
}
