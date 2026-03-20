import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '../stores/authStore'
import { InlineError } from '../components/ui/InlineError'
import { inputClass } from '../styles/classes'

export default function LoginPage() {
  const navigate = useNavigate()
  const { login, register } = useAuthStore()
  const [isRegister, setIsRegister] = useState(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      if (isRegister) {
        await register(email, displayName, password)
      } else {
        await login(email, password)
      }
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Authentication failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 relative overflow-hidden">
      {/* Background glow */}
      <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
        <div className="w-[600px] h-[600px] bg-indigo-500/5 rounded-full blur-3xl" />
      </div>

      <div className="w-full max-w-sm mx-4 md:mx-0 p-6 bg-gray-900 rounded-xl border border-gray-800 shadow-elevated relative animate-scale-in">
        {/* Branding */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-indigo-500/20 to-indigo-600/10 border border-indigo-500/20 flex items-center justify-center mb-4">
            <span className="text-lg font-bold text-indigo-400">A</span>
          </div>
          <h1 className="text-lg font-semibold text-white">Agent Platform</h1>
          <p className="text-sm text-gray-500 mt-1">
            {isRegister ? 'Create your account' : 'Sign in to your workspace'}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {isRegister && (
            <div>
              <label className="block text-xs text-gray-500 mb-1.5">Display name</label>
              <input
                type="text"
                placeholder="Your name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className={inputClass}
                required
              />
            </div>
          )}
          <div>
            <label className="block text-xs text-gray-500 mb-1.5">Email address</label>
            <input
              type="email"
              placeholder="you@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={inputClass}
              required
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1.5">Password</label>
            <input
              type="password"
              placeholder="Enter your password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={inputClass}
              required
            />
          </div>

          {error && <InlineError message={error} onDismiss={() => setError('')} />}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 bg-indigo-600 hover:bg-indigo-500 hover:shadow-glow-indigo disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-white text-sm font-medium transition-all flex items-center justify-center gap-2"
          >
            {loading && (
              <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                <path className="opacity-80" d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
              </svg>
            )}
            {isRegister ? 'Create Account' : 'Sign In'}
          </button>
        </form>

        <p className="mt-5 text-center text-sm text-gray-500">
          {isRegister ? 'Already have an account? ' : "Don't have an account? "}
          <button
            onClick={() => { setIsRegister(!isRegister); setError('') }}
            className="text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            {isRegister ? 'Sign in' : 'Register'}
          </button>
        </p>
      </div>
    </div>
  )
}
