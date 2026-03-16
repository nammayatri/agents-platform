import { useState } from 'react'
import { projects as projectsApi } from '../../services/api'
import { inputClass, selectClass } from '../../styles/classes'

interface ProjectBuildMergeTabProps {
  projectId: string
  buildCommands: string[]
  setBuildCommands: (cmds: string[]) => void
  mergeMethod: 'merge' | 'squash' | 'rebase'
  setMergeMethod: (method: 'merge' | 'squash' | 'rebase') => void
  requireMergeApproval: boolean
  setRequireMergeApproval: (v: boolean) => void
  setError: (err: string) => void
}

export default function ProjectBuildMergeTab({
  projectId, buildCommands, setBuildCommands, mergeMethod, setMergeMethod,
  requireMergeApproval, setRequireMergeApproval, setError,
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
        <label className="flex items-center gap-3 cursor-pointer group">
          <div className="relative">
            <input
              type="checkbox"
              checked={requireMergeApproval}
              onChange={(e) => setRequireMergeApproval(e.target.checked)}
              className="sr-only peer"
            />
            <div className="w-9 h-5 bg-gray-800 border border-gray-700 rounded-full peer-checked:bg-indigo-600 peer-checked:border-indigo-500 transition-colors" />
            <div className="absolute top-0.5 left-0.5 w-4 h-4 bg-gray-500 rounded-full peer-checked:translate-x-4 peer-checked:bg-white transition-all" />
          </div>
          <div>
            <span className="text-sm text-gray-300 group-hover:text-white transition-colors">
              Require approval before merge
            </span>
            <p className="text-[11px] text-gray-600">
              When enabled, the merge agent will pause and wait for your approval before merging PRs.
            </p>
          </div>
        </label>
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
              await projectsApi.buildSettings.update(projectId, {
                build_commands: cleanedCmds,
                merge_method: mergeMethod,
                require_merge_approval: requireMergeApproval,
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
