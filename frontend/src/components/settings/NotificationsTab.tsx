import { useEffect, useState } from 'react'
import { notifications as notificationsApi } from '../../services/api'
import type { NotificationChannel } from '../../types'
import { inputClass, btnPrimary, btnSecondary, btnDanger } from '../../styles/classes'

interface Props {
  isAdmin: boolean
}

const notifEventOptions = ['stuck', 'failed', 'completed', 'review', 'in_progress']

export default function NotificationsTab({ isAdmin: _isAdmin }: Props) {
  const [notifList, setNotifList] = useState<NotificationChannel[]>([])
  const [showNotifForm, setShowNotifForm] = useState(false)
  const [editingNotifId, setEditingNotifId] = useState<string | null>(null)
  const [notifForm, setNotifForm] = useState({
    channel_type: 'email',
    display_name: '',
    config_value: '', // email address, webhook URL, or slack webhook URL
    notify_on: ['stuck', 'failed', 'completed', 'review'] as string[],
  })
  const [testingNotifId, setTestingNotifId] = useState<string | null>(null)
  const [notifTestResult, setNotifTestResult] = useState<{ id: string; status: string; detail?: string } | null>(null)

  useEffect(() => {
    notificationsApi.list().then((n) => setNotifList(n as NotificationChannel[])).catch(() => {})
  }, [])

  const resetNotifForm = () => {
    setNotifForm({ channel_type: 'email', display_name: '', config_value: '', notify_on: ['stuck', 'failed', 'completed', 'review'] })
    setEditingNotifId(null)
    setShowNotifForm(false)
  }

  const startEditNotif = (n: NotificationChannel) => {
    const config = typeof n.config_json === 'string' ? JSON.parse(n.config_json) : n.config_json
    let configValue = ''
    if (n.channel_type === 'email') configValue = config.email || ''
    else if (n.channel_type === 'slack') configValue = config.webhook_url || ''
    else if (n.channel_type === 'webhook') configValue = config.url || ''

    setNotifForm({
      channel_type: n.channel_type,
      display_name: n.display_name,
      config_value: configValue,
      notify_on: n.notify_on || [],
    })
    setEditingNotifId(n.id)
    setShowNotifForm(true)
  }

  const buildNotifConfig = (): Record<string, string> => {
    const val = notifForm.config_value.trim()
    switch (notifForm.channel_type) {
      case 'email': return { email: val }
      case 'slack': return { webhook_url: val }
      case 'webhook': return { url: val }
      default: return {}
    }
  }

  const handleSaveNotif = async () => {
    const data = {
      channel_type: notifForm.channel_type,
      display_name: notifForm.display_name,
      config_json: buildNotifConfig(),
      notify_on: notifForm.notify_on,
    }
    if (editingNotifId) {
      await notificationsApi.update(editingNotifId, data)
    } else {
      await notificationsApi.create(data)
    }
    resetNotifForm()
    const updated = await notificationsApi.list()
    setNotifList(updated as NotificationChannel[])
  }

  const handleDeleteNotif = async (id: string) => {
    if (!confirm('Delete this notification channel?')) return
    await notificationsApi.delete(id)
    setNotifList(notifList.filter((n) => n.id !== id))
  }

  const handleToggleNotifActive = async (n: NotificationChannel) => {
    await notificationsApi.update(n.id, { is_active: !n.is_active })
    setNotifList(notifList.map((x) => (x.id === n.id ? { ...x, is_active: !n.is_active } : x)))
  }

  const handleTestNotif = async (id: string) => {
    setTestingNotifId(id)
    setNotifTestResult(null)
    try {
      const result = await notificationsApi.test(id)
      setNotifTestResult({ id, status: result.status, detail: result.detail })
    } catch (err) {
      setNotifTestResult({ id, status: 'error', detail: err instanceof Error ? err.message : 'Request failed' })
    } finally {
      setTestingNotifId(null)
    }
  }

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="text-sm font-medium text-gray-300 uppercase tracking-wider">Notification Channels</h2>
          <p className="text-xs text-gray-600 mt-1">
            Get notified when tasks complete, fail, or need your attention.
          </p>
        </div>
        {!showNotifForm && (
          <button
            onClick={() => {
              resetNotifForm()
              setShowNotifForm(true)
            }}
            className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            Add channel
          </button>
        )}
      </div>

      <div className="space-y-2 mb-3">
        {notifList.map((n) => {
          const config = typeof n.config_json === 'string' ? JSON.parse(n.config_json) : (n.config_json || {})
          const configDetail =
            n.channel_type === 'email' ? config.email :
            n.channel_type === 'slack' ? 'Slack webhook' :
            config.url || 'webhook'

          return (
            <div key={n.id}>
              <div className="p-3 bg-gray-900 border border-gray-800 rounded-lg">
                <div className="flex items-center justify-between">
                  <div className="flex-1 min-w-0 mr-3">
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-white font-medium">{n.display_name}</span>
                      <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500">
                        {n.channel_type}
                      </span>
                      {!n.is_active && (
                        <span className="px-1.5 py-0.5 bg-red-900/30 rounded text-[10px] text-red-400">
                          disabled
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-gray-500 mt-0.5">{configDetail}</div>
                    <div className="flex gap-1 mt-1.5">
                      {(n.notify_on || []).map((evt) => (
                        <span key={evt} className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500">
                          {evt}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex gap-2 items-center">
                    <button
                      onClick={() => handleToggleNotifActive(n)}
                      className={`px-2 py-1 text-xs transition-colors ${
                        n.is_active ? 'text-green-400 hover:text-green-300' : 'text-gray-600 hover:text-gray-400'
                      }`}
                    >
                      {n.is_active ? 'On' : 'Off'}
                    </button>
                    <button
                      onClick={() => handleTestNotif(n.id)}
                      disabled={testingNotifId === n.id}
                      className="px-2 py-1 text-xs text-gray-400 hover:text-white transition-colors disabled:opacity-50"
                    >
                      {testingNotifId === n.id ? 'Sending...' : 'Test'}
                    </button>
                    <button
                      onClick={() => startEditNotif(n)}
                      className="px-2 py-1 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
                    >
                      Edit
                    </button>
                    <button onClick={() => handleDeleteNotif(n.id)} className={btnDanger}>
                      Delete
                    </button>
                  </div>
                </div>
                {notifTestResult && notifTestResult.id === n.id && (
                  <div
                    className={`mt-2 px-3 py-2 rounded text-xs ${
                      notifTestResult.status === 'ok'
                        ? 'bg-green-900/30 text-green-400 border border-green-800/50'
                        : 'bg-red-900/30 text-red-400 border border-red-800/50'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium">
                        {notifTestResult.status === 'ok' ? 'Test sent successfully' : 'Test failed'}
                      </span>
                      <button
                        onClick={() => setNotifTestResult(null)}
                        className="text-gray-500 hover:text-gray-300 ml-2"
                      >
                        x
                      </button>
                    </div>
                    {notifTestResult.detail && (
                      <div className="mt-1 text-[11px] opacity-80 font-mono break-all">{notifTestResult.detail}</div>
                    )}
                  </div>
                )}
              </div>
              {/* Inline edit form */}
              {editingNotifId === n.id && showNotifForm && (
                <div className="mt-1 p-4 bg-gray-900 border border-indigo-900/50 rounded-lg space-y-3">
                  <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">Edit Channel</div>
                  <div className="grid grid-cols-3 gap-3">
                    <select
                      value={notifForm.channel_type}
                      onChange={(e) => setNotifForm({ ...notifForm, channel_type: e.target.value })}
                      className={inputClass}
                    >
                      <option value="email">Email</option>
                      <option value="slack">Slack</option>
                      <option value="webhook">Webhook</option>
                    </select>
                    <div className="col-span-2">
                      <input
                        className={inputClass}
                        placeholder="Display name"
                        value={notifForm.display_name}
                        onChange={(e) => setNotifForm({ ...notifForm, display_name: e.target.value })}
                      />
                    </div>
                  </div>
                  <input
                    className={inputClass}
                    placeholder={
                      notifForm.channel_type === 'email' ? 'Email address' :
                      notifForm.channel_type === 'slack' ? 'Slack webhook URL' :
                      'Webhook URL'
                    }
                    value={notifForm.config_value}
                    onChange={(e) => setNotifForm({ ...notifForm, config_value: e.target.value })}
                  />
                  <div>
                    <div className="text-xs text-gray-500 mb-1.5">Notify on events:</div>
                    <div className="flex gap-2 flex-wrap">
                      {notifEventOptions.map((evt) => (
                        <label key={evt} className="flex items-center gap-1.5 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={notifForm.notify_on.includes(evt)}
                            onChange={(e) => {
                              if (e.target.checked) {
                                setNotifForm({ ...notifForm, notify_on: [...notifForm.notify_on, evt] })
                              } else {
                                setNotifForm({ ...notifForm, notify_on: notifForm.notify_on.filter((x) => x !== evt) })
                              }
                            }}
                            className="rounded border-gray-700 bg-gray-800 text-indigo-500 focus:ring-indigo-500 focus:ring-offset-0"
                          />
                          <span className="text-xs text-gray-400">{evt}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button onClick={handleSaveNotif} className={btnPrimary}>Update</button>
                    <button onClick={resetNotifForm} className={btnSecondary}>Cancel</button>
                  </div>
                </div>
              )}
            </div>
          )
        })}
        {notifList.length === 0 && (
          <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
            No notification channels configured. Add one to get alerts for task events.
          </div>
        )}
      </div>

      {/* Add new notification channel form */}
      {showNotifForm && !editingNotifId && (
        <div className="p-4 bg-gray-900 border border-gray-800 rounded-lg space-y-3">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">New Notification Channel</div>
          <div className="grid grid-cols-3 gap-3">
            <select
              value={notifForm.channel_type}
              onChange={(e) => setNotifForm({ ...notifForm, channel_type: e.target.value })}
              className={inputClass}
            >
              <option value="email">Email</option>
              <option value="slack">Slack</option>
              <option value="webhook">Webhook</option>
            </select>
            <div className="col-span-2">
              <input
                className={inputClass}
                placeholder="Display name (e.g. Work Email, Team Slack)"
                value={notifForm.display_name}
                onChange={(e) => setNotifForm({ ...notifForm, display_name: e.target.value })}
              />
            </div>
          </div>
          <input
            className={inputClass}
            placeholder={
              notifForm.channel_type === 'email' ? 'Email address' :
              notifForm.channel_type === 'slack' ? 'Slack webhook URL (https://hooks.slack.com/...)' :
              'Webhook URL (POST requests with JSON body)'
            }
            value={notifForm.config_value}
            onChange={(e) => setNotifForm({ ...notifForm, config_value: e.target.value })}
          />
          <div>
            <div className="text-xs text-gray-500 mb-1.5">Notify on events:</div>
            <div className="flex gap-2 flex-wrap">
              {notifEventOptions.map((evt) => (
                <label key={evt} className="flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={notifForm.notify_on.includes(evt)}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setNotifForm({ ...notifForm, notify_on: [...notifForm.notify_on, evt] })
                      } else {
                        setNotifForm({ ...notifForm, notify_on: notifForm.notify_on.filter((x) => x !== evt) })
                      }
                    }}
                    className="rounded border-gray-700 bg-gray-800 text-indigo-500 focus:ring-indigo-500 focus:ring-offset-0"
                  />
                  <span className="text-xs text-gray-400">{evt}</span>
                </label>
              ))}
            </div>
          </div>
          <div className="flex gap-2">
            <button onClick={handleSaveNotif} className={btnPrimary}>Save</button>
            <button onClick={resetNotifForm} className={btnSecondary}>Cancel</button>
          </div>
        </div>
      )}
    </section>
  )
}
