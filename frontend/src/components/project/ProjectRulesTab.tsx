import { useState } from 'react'
import { projects as projectsApi } from '../../services/api'
import type { WorkRules } from '../../types'
import { inputClass } from '../../styles/classes'

interface ProjectRulesTabProps {
  projectId: string
  workRules: WorkRules
  setWorkRules: (rules: WorkRules) => void
  setError: (err: string) => void
}

export default function ProjectRulesTab({ projectId, workRules, setWorkRules, setError }: ProjectRulesTabProps) {
  const [rulesSaving, setRulesSaving] = useState(false)

  return (
    <>
      <div>
        <p className="text-sm text-gray-300">Work Rules</p>
        <p className="text-[11px] text-gray-600 mt-0.5">
          Define rules that guide AI agents when working on tasks. Quality rules are shell commands run after each iteration.
        </p>
      </div>
      {(['coding', 'testing', 'review', 'quality', 'general'] as const).map((category) => (
        <div key={category}>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-xs text-gray-500 capitalize">{category}</label>
            <button
              onClick={() => setWorkRules({ ...workRules, [category]: [...(workRules[category] || []), ''] })}
              className="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors"
            >
              + Add
            </button>
          </div>
          {(workRules[category] || []).length === 0 ? (
            <p className="text-[11px] text-gray-700 italic">No {category} rules</p>
          ) : (
            <div className="space-y-1">
              {(workRules[category] || []).map((rule, i) => (
                <div key={i} className="flex gap-1.5">
                  <input
                    className={`${inputClass} flex-1`}
                    placeholder={category === 'quality' ? 'e.g. ruff check . or npm test' : `${category} rule...`}
                    value={rule}
                    onChange={(e) => {
                      const updated = [...(workRules[category] || [])]
                      updated[i] = e.target.value
                      setWorkRules({ ...workRules, [category]: updated })
                    }}
                  />
                  <button
                    onClick={() => {
                      const updated = (workRules[category] || []).filter((_, idx) => idx !== i)
                      setWorkRules({ ...workRules, [category]: updated })
                    }}
                    className="px-2 text-gray-600 hover:text-red-400 transition-colors text-xs shrink-0"
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
      <div className="pt-2">
        <button
          onClick={async () => {
            if (!projectId) return
            setRulesSaving(true)
            setError('')
            try {
              // Filter out empty strings
              const cleaned: Record<string, string[]> = {}
              for (const [cat, items] of Object.entries(workRules)) {
                const filtered = (items || []).filter((r: string) => r.trim())
                if (filtered.length > 0) cleaned[cat] = filtered
              }
              const result = await projectsApi.rules.update(projectId, cleaned)
              setWorkRules(result as WorkRules)
            } catch (e) {
              setError(e instanceof Error ? e.message : 'Failed to save rules')
            } finally {
              setRulesSaving(false)
            }
          }}
          disabled={rulesSaving}
          className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
        >
          {rulesSaving ? 'Saving...' : 'Save Rules'}
        </button>
      </div>
    </>
  )
}
