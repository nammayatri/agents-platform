interface InlineErrorProps {
  message: string
  className?: string
}

export function InlineError({ message, className = '' }: InlineErrorProps) {
  if (!message) return null
  return (
    <div className={`px-4 py-2.5 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-sm ${className}`}>
      {message}
    </div>
  )
}
