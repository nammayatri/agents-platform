import { useState } from 'react'
import { projects as projectsApi } from '../../services/api'
import MarkdownEditor from '../ui/MarkdownEditor'

interface ProjectPlanningTabProps {
  projectId: string
  planningGuidelines: string
  setPlanningGuidelines: (v: string) => void
  requirePlanApproval: boolean
  setRequirePlanApproval: (v: boolean) => void
  setError: (err: string) => void
}

export default function ProjectPlanningTab({
  projectId, planningGuidelines, setPlanningGuidelines,
  requirePlanApproval, setRequirePlanApproval, setError,
}: ProjectPlanningTabProps) {
  const [saving, setSaving] = useState(false)

  const handleSave = async () => {
    setSaving(true)
    setError('')
    try {
      // Save both via build-settings (backwards compat routes through new structure)
      await projectsApi.buildSettings.update(projectId, {
        require_plan_approval: requirePlanApproval,
      })

      // Save guidelines via unified settings section endpoint
      await projectsApi.updateSettingsSection(projectId, 'planning', {
        guidelines: planningGuidelines,
        require_approval: requirePlanApproval,
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <div>
        <p className="text-sm text-gray-300">Planning Configuration</p>
        <p className="text-[11px] text-gray-600 mt-0.5">
          Control how the AI planner decomposes tasks into sub-tasks.
        </p>
      </div>

      {/* Plan Approval Toggle */}
      <div>
        <label className="flex items-center gap-3 cursor-pointer group">
          <div className="relative">
            <input
              type="checkbox"
              checked={requirePlanApproval}
              onChange={(e) => setRequirePlanApproval(e.target.checked)}
              className="sr-only peer"
            />
            <div className="w-9 h-5 bg-gray-800 border border-gray-700 rounded-full peer-checked:bg-indigo-600 peer-checked:border-indigo-500 transition-colors" />
            <div className="absolute top-0.5 left-0.5 w-4 h-4 bg-gray-500 rounded-full peer-checked:translate-x-4 peer-checked:bg-white transition-all" />
          </div>
          <div>
            <span className="text-sm text-gray-300 group-hover:text-white transition-colors">
              Require approval before executing plans
            </span>
            <p className="text-[11px] text-gray-600">
              When enabled, plans pause for your review before execution begins.
            </p>
          </div>
        </label>
      </div>

      {/* Planning Guidelines */}
      <div>
        <label className="block text-xs text-gray-500 mb-1.5">Planning Guidelines</label>
        <MarkdownEditor
          value={planningGuidelines}
          onChange={setPlanningGuidelines}
          placeholder={`# Planning Guidelines

- Always create separate sub-tasks for database migrations
- Use the repository pattern for data access
- Never modify files in the \`/legacy/\` directory
- All API changes must include a tester sub-task
- Prefer small, focused sub-tasks over large ones

## Architecture Constraints
- New endpoints go in \`src/api/routes/\`
- Shared types in \`src/types/\``}
          minHeight={200}
          maxHeight={500}
        />
        <p className="text-[11px] text-gray-600 mt-1.5">
          These guidelines are injected into the planner's system prompt for every task in this project.
          Use them to enforce architectural decisions, coding patterns, or workflow rules.
        </p>
      </div>

      {/* Save */}
      <div className="pt-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
        >
          {saving ? 'Saving...' : 'Save Planning Settings'}
        </button>
      </div>
    </>
  )
}
