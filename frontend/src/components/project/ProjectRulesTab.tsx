import { useState } from 'react'
import { projects as projectsApi } from '../../services/api'
import { inputClass } from '../../styles/classes'
import MarkdownEditor from '../ui/MarkdownEditor'
import type { WorkRules } from '../../types'

interface ProjectRulesTabProps {
  projectId: string
  workRules: WorkRules
  setWorkRules: (rules: WorkRules) => void
  setError: (err: string) => void
}

const RULE_CATEGORIES = [
  {
    key: 'coding',
    label: 'Coding Rules',
    hint: 'Standards and patterns for code agents. Written as markdown — use headings, lists, code blocks.',
    placeholder: `# Coding Standards

- Use TypeScript strict mode for all new files
- Follow the repository pattern for data access
- Never modify files in \`/legacy/\` directory
- Use \`async/await\` over raw promises

## Naming Conventions
- React components: PascalCase
- Utilities: camelCase
- Database columns: snake_case`,
    useEditor: true,
  },
  {
    key: 'testing',
    label: 'Testing Rules',
    hint: 'Guidelines for the tester agent on how to write and run tests.',
    placeholder: `# Testing Guidelines

- Write unit tests for all new functions
- Integration tests for API endpoints
- Use \`pytest\` fixtures, not setUp/tearDown
- Mock external services, never call real APIs in tests`,
    useEditor: true,
  },
  {
    key: 'review',
    label: 'Review Rules',
    hint: 'Criteria for the reviewer agent when evaluating code changes.',
    placeholder: `# Review Criteria

- Check for proper error handling
- Ensure no hardcoded secrets or credentials
- Verify backward compatibility
- Flag any N+1 query patterns`,
    useEditor: true,
  },
  {
    key: 'quality',
    label: 'Quality Check Commands',
    hint: 'Shell commands run after each coding iteration. If any command fails (non-zero exit), the agent retries.',
    placeholder: '',
    useEditor: false,  // quality rules are shell commands, keep as list
  },
  {
    key: 'general',
    label: 'General Rules',
    hint: 'Rules applied to all agent types.',
    placeholder: `# General Rules

- Always read existing code before modifying
- Prefer small, focused changes over large rewrites
- Add comments only where logic is non-obvious`,
    useEditor: true,
  },
] as const

function rulesArrayToMarkdown(rules: string[]): string {
  if (!rules || rules.length === 0) return ''
  // If it looks like it was already markdown (has newlines), join with double newlines
  if (rules.length === 1) return rules[0]
  // Multiple rules stored as array → convert to bullet list
  return rules.map(r => r.includes('\n') ? r : `- ${r}`).join('\n')
}

function markdownToRulesArray(md: string): string[] {
  const trimmed = md.trim()
  if (!trimmed) return []
  // Store the entire markdown as a single entry
  return [trimmed]
}

export default function ProjectRulesTab({ projectId, workRules, setWorkRules, setError }: ProjectRulesTabProps) {
  const [rulesSaving, setRulesSaving] = useState(false)
  const [activeCategory, setActiveCategory] = useState<string>(RULE_CATEGORIES[0].key)

  const currentCategory = RULE_CATEGORIES.find(c => c.key === activeCategory)!

  const handleSave = async () => {
    if (!projectId) return
    setRulesSaving(true)
    setError('')
    try {
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
  }

  return (
    <>
      <div>
        <p className="text-sm text-gray-300">Work Rules</p>
        <p className="text-[11px] text-gray-600 mt-0.5">
          Define rules that guide AI agents. Write in markdown for rich formatting. Quality rules are shell commands run after each iteration.
        </p>
      </div>

      {/* Category tabs */}
      <div className="flex gap-1 border-b border-gray-800 overflow-x-auto">
        {RULE_CATEGORIES.map((cat) => {
          const hasContent = (workRules[cat.key] || []).some(r => r.trim())
          return (
            <button
              key={cat.key}
              onClick={() => setActiveCategory(cat.key)}
              className={`px-3 py-1.5 text-xs whitespace-nowrap border-b-2 transition-colors ${
                activeCategory === cat.key
                  ? 'border-indigo-500 text-indigo-400'
                  : 'border-transparent text-gray-500 hover:text-gray-300'
              }`}
            >
              {cat.label}
              {hasContent && <span className="ml-1.5 w-1.5 h-1.5 rounded-full bg-indigo-400 inline-block" />}
            </button>
          )
        })}
      </div>

      {/* Active category editor */}
      <div>
        <p className="text-[11px] text-gray-600 mb-2">{currentCategory.hint}</p>

        {currentCategory.useEditor ? (
          <MarkdownEditor
            value={rulesArrayToMarkdown(workRules[currentCategory.key] || [])}
            onChange={(md) => {
              setWorkRules({
                ...workRules,
                [currentCategory.key]: markdownToRulesArray(md),
              })
            }}
            placeholder={currentCategory.placeholder}
            minHeight={200}
            maxHeight={500}
          />
        ) : (
          /* Quality commands — keep as list of shell commands */
          <div className="space-y-1.5">
            {(workRules[currentCategory.key] || []).map((rule, i) => (
              <div key={i} className="flex gap-1.5">
                <input
                  className={`${inputClass} flex-1 font-mono text-xs`}
                  placeholder="e.g. ruff check . or npm test"
                  value={rule}
                  onChange={(e) => {
                    const updated = [...(workRules[currentCategory.key] || [])]
                    updated[i] = e.target.value
                    setWorkRules({ ...workRules, [currentCategory.key]: updated })
                  }}
                />
                <button
                  onClick={() => {
                    const updated = (workRules[currentCategory.key] || []).filter((_, idx) => idx !== i)
                    setWorkRules({ ...workRules, [currentCategory.key]: updated })
                  }}
                  className="px-2 text-gray-600 hover:text-red-400 transition-colors text-xs shrink-0"
                >
                  Remove
                </button>
              </div>
            ))}
            <button
              onClick={() => setWorkRules({
                ...workRules,
                [currentCategory.key]: [...(workRules[currentCategory.key] || []), ''],
              })}
              className="text-[11px] text-indigo-400 hover:text-indigo-300 transition-colors"
            >
              + Add Command
            </button>
          </div>
        )}
      </div>

      <div className="pt-2">
        <button
          onClick={handleSave}
          disabled={rulesSaving}
          className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
        >
          {rulesSaving ? 'Saving...' : 'Save Rules'}
        </button>
      </div>
    </>
  )
}
