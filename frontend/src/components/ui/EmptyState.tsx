interface EmptyStateProps {
  message: string
  className?: string
}

export function EmptyState({ message, className = '' }: EmptyStateProps) {
  return (
    <div className={`py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg ${className}`}>
      {message}
    </div>
  )
}
