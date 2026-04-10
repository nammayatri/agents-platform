import { useState, useEffect } from 'react'
import { projects as projectsApi } from '../../services/api'

interface ProjectSettingsJsonEditorProps {
  projectId: string
  settingsJson: Record<string, unknown> | null
  onSaved: (updated: Record<string, unknown>) => void
  setError: (err: string) => void
}

export default function ProjectSettingsJsonEditor({
  projectId, settingsJson, onSaved, setError,
}: ProjectSettingsJsonEditorProps) {
  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)
  const [parseError, setParseError] = useState('')

  useEffect(() => {
    setText(JSON.stringify(settingsJson || {}, null, 2))
  }, [settingsJson])

  const handleTextChange = (value: string) => {
    setText(value)
    try {
      JSON.parse(value)
      setParseError('')
    } catch (e) {
      setParseError(e instanceof Error ? e.message : 'Invalid JSON')
    }
  }

  const handleSave = async () => {
    let parsed: Record<string, unknown>
    try {
      parsed = JSON.parse(text)
    } catch (e) {
      setParseError(e instanceof Error ? e.message : 'Invalid JSON')
      return
    }

    setSaving(true)
    setError('')
    try {
      await projectsApi.update(projectId, { settings_json: parsed } as never)
      onSaved(parsed)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm text-gray-300">Raw Settings JSON</p>
        <p className="text-[11px] text-gray-600 mt-0.5">
          Direct edit of the project settings_json. Changes here affect all settings.
        </p>
      </div>

      <div className="relative">
        <textarea
          value={text}
          onChange={(e) => handleTextChange(e.target.value)}
          spellCheck={false}
          rows={30}
          className={`w-full px-4 py-3 bg-gray-900 border rounded-lg text-sm text-gray-300 font-mono leading-relaxed focus:outline-none transition-colors resize-y ${
            parseError ? 'border-red-500/50 focus:border-red-500' : 'border-gray-800 focus:border-indigo-500'
          }`}
        />
        {parseError && (
          <p className="text-xs text-red-400 mt-1">{parseError}</p>
        )}
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saving || !!parseError}
          className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
        >
          {saving ? 'Saving...' : 'Save JSON'}
        </button>
        <button
          onClick={() => {
            setText(JSON.stringify(settingsJson || {}, null, 2))
            setParseError('')
          }}
          className="px-4 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors"
        >
          Reset
        </button>
      </div>
    </div>
  )
}
