import { AlertCircle, AlertTriangle, Info, X } from 'lucide-react'

interface InlineErrorProps {
  message: string
  className?: string
  onDismiss?: () => void
  variant?: 'error' | 'warning' | 'info'
}

const VARIANT_STYLES = {
  error:   { bg: 'bg-red-500/10', border: 'border-red-500/20', text: 'text-red-400', Icon: AlertCircle },
  warning: { bg: 'bg-amber-500/10', border: 'border-amber-500/20', text: 'text-amber-400', Icon: AlertTriangle },
  info:    { bg: 'bg-blue-500/10', border: 'border-blue-500/20', text: 'text-blue-400', Icon: Info },
}

export function InlineError({ message, className = '', onDismiss, variant = 'error' }: InlineErrorProps) {
  if (!message) return null
  const style = VARIANT_STYLES[variant]

  return (
    <div className={`flex items-start gap-2.5 px-4 py-2.5 ${style.bg} border ${style.border} rounded-lg animate-fade-in ${className}`}>
      <style.Icon className={`w-4 h-4 ${style.text} shrink-0 mt-0.5`} />
      <span className={`${style.text} text-sm flex-1`}>{message}</span>
      {onDismiss && (
        <button onClick={onDismiss} className="text-gray-600 hover:text-gray-400 transition-colors shrink-0">
          <X className="w-3.5 h-3.5" />
        </button>
      )}
    </div>
  )
}
