import type { ReactNode } from 'react'

interface EmptyStateProps {
  icon?: ReactNode
  title?: string
  message: string
  action?: { label: string; onClick: () => void }
  className?: string
  size?: 'sm' | 'md' | 'lg'
}

export function EmptyState({ icon, title, message, action, className = '', size = 'md' }: EmptyStateProps) {
  const padding = size === 'sm' ? 'py-6' : size === 'lg' ? 'py-16' : 'py-10'
  const iconBox = size === 'sm' ? 'w-12 h-12 rounded-lg' : size === 'lg' ? 'w-20 h-20 rounded-2xl' : 'w-16 h-16 rounded-xl'
  const iconSize = size === 'sm' ? '[&>svg]:w-5 [&>svg]:h-5' : size === 'lg' ? '[&>svg]:w-8 [&>svg]:h-8' : '[&>svg]:w-6 [&>svg]:h-6'

  return (
    <div className={`flex flex-col items-center justify-center text-center animate-fade-in ${padding} ${className}`}>
      {icon && (
        <div className={`${iconBox} bg-gray-800/50 flex items-center justify-center text-gray-600 mb-4 ${iconSize}`}>
          {icon}
        </div>
      )}
      {title && <p className="text-sm font-medium text-white mb-1">{title}</p>}
      <p className="text-sm text-gray-500 max-w-xs">{message}</p>
      {action && (
        <button
          onClick={action.onClick}
          className="mt-4 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-sm font-medium text-white transition-colors"
        >
          {action.label}
        </button>
      )}
    </div>
  )
}
