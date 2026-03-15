import { useState } from 'react'
import { projects as projectsApi } from '../../services/api'
import type { ProjectMember } from '../../types'
import { inputClass } from '../../styles/classes'

interface ProjectMembersTabProps {
  projectId: string
  userRole: 'owner' | 'member'
  memberOwner: ProjectMember | null
  members: ProjectMember[]
  setMembers: (members: ProjectMember[]) => void
}

export default function ProjectMembersTab({ projectId, userRole, memberOwner, members, setMembers }: ProjectMembersTabProps) {
  const [memberEmail, setMemberEmail] = useState('')
  const [memberLoading, setMemberLoading] = useState(false)
  const [memberError, setMemberError] = useState('')

  return (
    <>
      <div>
        <p className="text-sm text-gray-300">Team Members</p>
        <p className="text-[11px] text-gray-600 mt-0.5">Manage who has access to this project. Members can view the project and create tasks.</p>
      </div>

      {/* Owner card */}
      {memberOwner && (
        <div className="flex items-center gap-3 px-3 py-2.5 bg-gray-900 border border-gray-800 rounded-lg">
          <div className="w-8 h-8 rounded-full bg-indigo-500/20 flex items-center justify-center text-xs font-semibold text-indigo-400 shrink-0">
            {memberOwner.display_name?.charAt(0)?.toUpperCase() || memberOwner.email.charAt(0).toUpperCase()}
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm text-white">{memberOwner.display_name || memberOwner.email}</p>
            <p className="text-xs text-gray-500">{memberOwner.email}</p>
          </div>
          <span className="px-2 py-0.5 bg-indigo-500/10 border border-indigo-500/20 rounded text-[10px] text-indigo-400 font-medium">Owner</span>
        </div>
      )}

      {/* Member list */}
      {members.length > 0 && (
        <div className="space-y-1.5">
          {members.map((m) => (
            <div key={m.id} className="flex items-center gap-3 px-3 py-2.5 bg-gray-900 border border-gray-800 rounded-lg">
              <div className="w-8 h-8 rounded-full bg-gray-800 flex items-center justify-center text-xs font-semibold text-gray-400 shrink-0">
                {m.display_name?.charAt(0)?.toUpperCase() || m.email.charAt(0).toUpperCase()}
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm text-white">{m.display_name || m.email}</p>
                <p className="text-xs text-gray-500">{m.email}</p>
              </div>
              <span className="px-2 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500 font-medium">Member</span>
              {userRole === 'owner' && (
                <button
                  onClick={async () => {
                    if (!confirm(`Remove ${m.display_name || m.email} from this project?`)) return
                    try {
                      await projectsApi.members.remove(projectId!, m.id)
                      setMembers(members.filter((x) => x.id !== m.id))
                    } catch (e) {
                      setMemberError(e instanceof Error ? e.message : 'Failed to remove member')
                    }
                  }}
                  className="text-xs text-gray-600 hover:text-red-400 transition-colors"
                >
                  Remove
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {members.length === 0 && (
        <div className="py-4 text-center text-xs text-gray-600 border border-dashed border-gray-800 rounded-lg">
          No members added yet.{userRole === 'owner' ? ' Add team members below.' : ''}
        </div>
      )}

      {/* Add member form (owner only) */}
      {userRole === 'owner' && (
        <div className="pt-2">
          <label className="block text-xs text-gray-500 mb-1">Add Member by Email</label>
          <div className="flex gap-2">
            <input
              className={inputClass}
              placeholder="user@example.com"
              value={memberEmail}
              onChange={(e) => { setMemberEmail(e.target.value); setMemberError('') }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && memberEmail.trim()) {
                  e.preventDefault()
                  document.getElementById('add-member-btn')?.click()
                }
              }}
            />
            <button
              id="add-member-btn"
              disabled={memberLoading || !memberEmail.trim()}
              onClick={async () => {
                setMemberLoading(true)
                setMemberError('')
                try {
                  const added = await projectsApi.members.add(projectId!, memberEmail.trim())
                  setMembers([...members, { ...added, added_at: new Date().toISOString() } as ProjectMember])
                  setMemberEmail('')
                } catch (e) {
                  setMemberError(e instanceof Error ? e.message : 'Failed to add member')
                } finally {
                  setMemberLoading(false)
                }
              }}
              className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 rounded-lg text-sm font-medium text-white transition-colors shrink-0"
            >
              {memberLoading ? 'Adding...' : 'Add'}
            </button>
          </div>
          {memberError && (
            <p className="text-xs text-red-400 mt-1.5">{memberError}</p>
          )}
          <p className="text-[11px] text-gray-600 mt-1">The user must already have an account.</p>
        </div>
      )}
    </>
  )
}
