import { useEffect, useState } from 'react'
import { admin as adminApi } from '../../services/api'
import { inputClass, btnPrimary, btnSecondary } from '../../styles/classes'

interface Props {
  isAdmin: boolean
}

export default function EmailConfigTab({ isAdmin }: Props) {
  const [emailConfig, setEmailConfig] = useState({
    smtp_host: 'smtp.gmail.com',
    smtp_port: '587',
    smtp_user: '',
    smtp_password: '',
    from_email: '',
    from_name: 'Agents Platform',
  })
  const [emailConfigSaving, setEmailConfigSaving] = useState(false)
  const [emailConfigStatus, setEmailConfigStatus] = useState<string | null>(null)

  useEffect(() => {
    if (isAdmin) {
      adminApi.getSetting('email').then((data: Record<string, unknown>) => {
        const val = (data.value_json || {}) as Record<string, string>
        if (val.smtp_host) setEmailConfig((prev) => ({ ...prev, ...val }))
      }).catch(() => {})
    }
  }, [isAdmin])

  if (!isAdmin) return null

  return (
    <section>
      <h2 className="text-lg font-semibold text-white mb-1">Email Configuration</h2>
      <p className="text-sm text-gray-500 mb-4">
        Configure the SMTP account used to send all email notifications. Users only provide their
        email address — this account is used for sending.
      </p>

      <div className="p-4 bg-gray-900 border border-gray-800 rounded-lg space-y-3">
        <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">SMTP Settings</div>
        <div className="grid grid-cols-3 gap-3">
          <div className="col-span-2">
            <label className="block text-xs text-gray-500 mb-1">SMTP Host</label>
            <input className={inputClass} placeholder="smtp.gmail.com" value={emailConfig.smtp_host} onChange={(e) => setEmailConfig({ ...emailConfig, smtp_host: e.target.value })} />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Port</label>
            <input className={inputClass} placeholder="587" value={emailConfig.smtp_port} onChange={(e) => setEmailConfig({ ...emailConfig, smtp_port: e.target.value })} />
          </div>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">SMTP Username</label>
          <input className={inputClass} placeholder="your-account@gmail.com" value={emailConfig.smtp_user} onChange={(e) => setEmailConfig({ ...emailConfig, smtp_user: e.target.value })} />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">App Password</label>
          <input className={inputClass} type="password" placeholder="Google app password" value={emailConfig.smtp_password} onChange={(e) => setEmailConfig({ ...emailConfig, smtp_password: e.target.value })} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">From Email (optional, defaults to username)</label>
            <input className={inputClass} placeholder="notifications@yourdomain.com" value={emailConfig.from_email} onChange={(e) => setEmailConfig({ ...emailConfig, from_email: e.target.value })} />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">From Name</label>
            <input className={inputClass} placeholder="Agents Platform" value={emailConfig.from_name} onChange={(e) => setEmailConfig({ ...emailConfig, from_name: e.target.value })} />
          </div>
        </div>
        <div className="flex items-center gap-3 pt-2">
          <button
            className={btnPrimary}
            disabled={emailConfigSaving}
            onClick={async () => {
              setEmailConfigSaving(true)
              setEmailConfigStatus(null)
              try {
                await adminApi.putSetting('email', emailConfig)
                setEmailConfigStatus('Saved')
              } catch (err) {
                setEmailConfigStatus(err instanceof Error ? err.message : 'Failed to save')
              } finally {
                setEmailConfigSaving(false)
              }
            }}
          >
            {emailConfigSaving ? 'Saving...' : 'Save'}
          </button>
          <button
            className={btnSecondary}
            onClick={async () => {
              setEmailConfigSaving(true)
              setEmailConfigStatus(null)
              try {
                const res = await fetch('/api/admin/settings/email/test', { method: 'POST', headers: { 'Authorization': `Bearer ${localStorage.getItem('token')}` } })
                const data = await res.json()
                setEmailConfigStatus(data.status === 'ok' ? 'Test email sent!' : `Error: ${data.detail}`)
              } catch (err) {
                setEmailConfigStatus(err instanceof Error ? err.message : 'Test failed')
              } finally {
                setEmailConfigSaving(false)
              }
            }}
          >
            Send Test Email
          </button>
          {emailConfigStatus && (
            <span className={`text-xs ${emailConfigStatus.startsWith('Error') ? 'text-red-400' : 'text-green-400'}`}>
              {emailConfigStatus}
            </span>
          )}
        </div>
      </div>

      <div className="mt-4 p-3 bg-gray-950 border border-gray-800 rounded-lg">
        <div className="text-xs text-gray-500">
          <strong className="text-gray-400">Google SMTP setup:</strong> Go to{' '}
          <span className="text-indigo-400">myaccount.google.com &gt; Security &gt; 2-Step Verification &gt; App passwords</span>,
          generate an app password, and use it above. Host: <code className="text-gray-400">smtp.gmail.com</code>, Port: <code className="text-gray-400">587</code>.
        </div>
      </div>
    </section>
  )
}
