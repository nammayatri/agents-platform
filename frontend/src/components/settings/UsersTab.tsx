import { useEffect, useState } from 'react'
import { admin as adminApi } from '../../services/api'
import { useAuthStore } from '../../stores/authStore'

interface Props {
  isAdmin: boolean
}

interface AdminUser {
  id: string
  email: string
  display_name: string
  role: string
  created_at: string
}

export default function UsersTab({ isAdmin }: Props) {
  const { user } = useAuthStore()
  const [users, setUsers] = useState<AdminUser[]>([])
  const [updatingUserId, setUpdatingUserId] = useState<string | null>(null)

  useEffect(() => {
    if (isAdmin) {
      adminApi.users().then(setUsers)
    }
  }, [isAdmin])

  const handleRoleToggle = async (targetUser: AdminUser) => {
    const newRole = targetUser.role === 'admin' ? 'user' : 'admin'
    const action = newRole === 'admin' ? 'promote to admin' : 'demote to regular user'
    if (!confirm(`Are you sure you want to ${action}: ${targetUser.email}?`)) return

    setUpdatingUserId(targetUser.id)
    try {
      await adminApi.updateUserRole(targetUser.id, newRole)
      setUsers(users.map((u) => (u.id === targetUser.id ? { ...u, role: newRole } : u)))
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update role')
    } finally {
      setUpdatingUserId(null)
    }
  }

  if (!isAdmin) return null

  return (
    <section>
      <h2 className="text-sm font-medium text-gray-300 uppercase tracking-wider mb-3">
        User Management
      </h2>
      <p className="text-xs text-gray-600 mb-3">
        Manage user roles. Admins can see all tasks, system stats, and audit logs.
      </p>

      <div className="space-y-2">
        {users.map((u) => {
          const isSelf = u.id === user?.id
          return (
            <div
              key={u.id}
              className="p-3 bg-gray-900 rounded-lg border border-gray-800 flex items-center justify-between"
            >
              <div className="flex items-center gap-3">
                <div
                  className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold ${
                    u.role === 'admin'
                      ? 'bg-amber-900 text-amber-300'
                      : 'bg-gray-700 text-gray-300'
                  }`}
                >
                  {u.display_name.charAt(0).toUpperCase()}
                </div>
                <div>
                  <div className="text-sm text-white font-medium">
                    {u.display_name}
                    {isSelf && <span className="ml-2 text-xs text-gray-500">(you)</span>}
                  </div>
                  <div className="text-xs text-gray-500">{u.email}</div>
                </div>
              </div>

              <div className="flex items-center gap-3">
                <span
                  className={`px-2 py-0.5 rounded text-xs font-medium ${
                    u.role === 'admin'
                      ? 'bg-amber-900/50 text-amber-300'
                      : 'bg-gray-700 text-gray-300'
                  }`}
                >
                  {u.role}
                </span>

                {!isSelf && (
                  <button
                    onClick={() => handleRoleToggle(u)}
                    disabled={updatingUserId === u.id}
                    className={`px-3 py-1 rounded text-xs font-medium disabled:opacity-50 ${
                      u.role === 'admin'
                        ? 'bg-red-900/50 text-red-300 hover:bg-red-900'
                        : 'bg-blue-900/50 text-blue-300 hover:bg-blue-900'
                    }`}
                  >
                    {updatingUserId === u.id
                      ? '...'
                      : u.role === 'admin'
                        ? 'Demote'
                        : 'Promote'}
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}
