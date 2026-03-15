import { useEffect, useState } from 'react'
import { skills as skillsApi } from '../../services/api'
import type { Skill } from '../../types'
import { inputClass, btnPrimary, btnSecondary, btnDanger } from '../../styles/classes'

interface Props {
  isAdmin: boolean
}

export default function SkillsTab({ isAdmin: _isAdmin }: Props) {
  const [skillList, setSkillList] = useState<Skill[]>([])
  const [showSkillForm, setShowSkillForm] = useState(false)
  const [editingSkillId, setEditingSkillId] = useState<string | null>(null)
  const [skillForm, setSkillForm] = useState({
    name: '',
    description: '',
    prompt: '',
    category: 'general',
  })

  useEffect(() => {
    skillsApi.list().then((s) => setSkillList(s as Skill[])).catch(() => {})
  }, [])

  const resetSkillForm = () => {
    setSkillForm({ name: '', description: '', prompt: '', category: 'general' })
    setEditingSkillId(null)
    setShowSkillForm(false)
  }

  const startEditSkill = (s: Skill) => {
    setSkillForm({
      name: s.name,
      description: s.description || '',
      prompt: s.prompt,
      category: s.category,
    })
    setEditingSkillId(s.id)
    setShowSkillForm(true)
  }

  const handleSaveSkill = async () => {
    if (editingSkillId) {
      const updated = await skillsApi.update(editingSkillId, skillForm) as Skill
      setSkillList(skillList.map((s) => (s.id === editingSkillId ? updated : s)))
    } else {
      const created = await skillsApi.create(skillForm) as Skill
      setSkillList([created, ...skillList])
    }
    resetSkillForm()
  }

  const handleDeleteSkill = async (id: string) => {
    if (!confirm('Delete this skill?')) return
    await skillsApi.delete(id)
    setSkillList(skillList.filter((s) => s.id !== id))
  }

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="text-sm font-medium text-gray-300 uppercase tracking-wider">Skills</h2>
          <p className="text-xs text-gray-600 mt-1">
            Reusable prompt-based capabilities that agents can use during task execution.
          </p>
        </div>
        {!showSkillForm && (
          <button
            onClick={() => {
              resetSkillForm()
              setShowSkillForm(true)
            }}
            className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            Add skill
          </button>
        )}
      </div>

      <div className="space-y-2 mb-3">
        {skillList.map((s) => (
          <div key={s.id}>
            <div className="p-3 bg-gray-900 border border-gray-800 rounded-lg flex items-center justify-between">
              <div className="flex-1 min-w-0 mr-3">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-white font-medium">{s.name}</span>
                  <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500">
                    {s.category}
                  </span>
                  {!s.is_active && (
                    <span className="px-1.5 py-0.5 bg-red-900/30 rounded text-[10px] text-red-400">
                      disabled
                    </span>
                  )}
                </div>
                {s.description && (
                  <div className="text-xs text-gray-500 mt-0.5 truncate">{s.description}</div>
                )}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => startEditSkill(s)}
                  className="px-2 py-1 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
                >
                  Edit
                </button>
                <button onClick={() => handleDeleteSkill(s.id)} className={btnDanger}>
                  Delete
                </button>
              </div>
            </div>
            {/* Inline edit form */}
            {editingSkillId === s.id && showSkillForm && (
              <div className="mt-1 p-4 bg-gray-900 border border-indigo-900/50 rounded-lg space-y-3">
                <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">Edit Skill</div>
                <div className="grid grid-cols-3 gap-3">
                  <div className="col-span-2">
                    <input className={inputClass} placeholder="Skill name" value={skillForm.name}
                      onChange={(e) => setSkillForm({ ...skillForm, name: e.target.value })} />
                  </div>
                  <select value={skillForm.category} onChange={(e) => setSkillForm({ ...skillForm, category: e.target.value })} className={inputClass}>
                    <option value="general">General</option>
                    <option value="coding">Coding</option>
                    <option value="testing">Testing</option>
                    <option value="docs">Documentation</option>
                    <option value="devops">DevOps</option>
                  </select>
                </div>
                <input className={inputClass} placeholder="Short description (optional)" value={skillForm.description}
                  onChange={(e) => setSkillForm({ ...skillForm, description: e.target.value })} />
                <textarea className={`${inputClass} resize-none`} placeholder="Skill prompt / instructions" rows={5}
                  value={skillForm.prompt} onChange={(e) => setSkillForm({ ...skillForm, prompt: e.target.value })} />
                <div className="flex gap-2">
                  <button onClick={handleSaveSkill} className={btnPrimary}>Update</button>
                  <button onClick={resetSkillForm} className={btnSecondary}>Cancel</button>
                </div>
              </div>
            )}
          </div>
        ))}
        {skillList.length === 0 && (
          <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
            No skills configured. Skills give agents specialized capabilities.
          </div>
        )}
      </div>

      {/* Add new skill form */}
      {showSkillForm && !editingSkillId && (
        <div className="p-4 bg-gray-900 border border-gray-800 rounded-lg space-y-3">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">New Skill</div>
          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-2">
              <input className={inputClass} placeholder="Skill name" value={skillForm.name}
                onChange={(e) => setSkillForm({ ...skillForm, name: e.target.value })} />
            </div>
            <select value={skillForm.category} onChange={(e) => setSkillForm({ ...skillForm, category: e.target.value })} className={inputClass}>
              <option value="general">General</option>
              <option value="coding">Coding</option>
              <option value="testing">Testing</option>
              <option value="docs">Documentation</option>
              <option value="devops">DevOps</option>
            </select>
          </div>
          <input className={inputClass} placeholder="Short description (optional)" value={skillForm.description}
            onChange={(e) => setSkillForm({ ...skillForm, description: e.target.value })} />
          <textarea className={`${inputClass} resize-none`}
            placeholder="Skill prompt / instructions (injected into agent context when this skill is active)" rows={5}
            value={skillForm.prompt} onChange={(e) => setSkillForm({ ...skillForm, prompt: e.target.value })} />
          <div className="flex gap-2">
            <button onClick={handleSaveSkill} className={btnPrimary}>Save</button>
            <button onClick={resetSkillForm} className={btnSecondary}>Cancel</button>
          </div>
        </div>
      )}
    </section>
  )
}
