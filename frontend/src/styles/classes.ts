/** Shared Tailwind class strings matching the design system in CLAUDE.md */

// ── Inputs ──────────────────────────────────────────────────────

export const inputClass =
  'w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors'

export const selectClass =
  'w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors'

// ── Buttons — compact (inline actions) ──────────────────────────

export const btnPrimary =
  'px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-sm text-white transition-colors'

export const btnSecondary =
  'px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors'

export const btnDanger =
  'px-2 py-1 text-xs text-red-400 hover:text-red-300 transition-colors'

// ── Buttons — large (primary form actions) ──────────────────────

export const btnPrimaryLg =
  'px-5 py-2 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-sm font-medium text-white transition-colors'

export const btnSecondaryLg =
  'px-5 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors'

// ── Buttons — icon-only ─────────────────────────────────────────

export const btnIcon =
  'p-2 rounded-lg text-gray-500 hover:text-gray-300 hover:bg-gray-800 transition-colors'

export const btnIconSm =
  'p-1.5 rounded-md text-gray-500 hover:text-gray-300 hover:bg-gray-800 transition-colors'

export const btnDisabled = 'disabled:opacity-40 disabled:cursor-not-allowed'

// ── Cards ───────────────────────────────────────────────────────

export const card =
  'bg-gray-900 border border-gray-800 rounded-lg'

export const cardHover =
  'bg-gray-900 border border-gray-800 rounded-lg hover:border-gray-700 transition-colors'

export const cardInteractive =
  'bg-gray-900 border border-gray-800 rounded-lg hover:border-gray-700 transition-all hover:shadow-card cursor-pointer'

// ── Badges ──────────────────────────────────────────────────────

export const badge = 'px-2 py-0.5 rounded text-[10px] font-medium'
export const badgeActive = 'bg-indigo-500/10 border border-indigo-500/20 text-indigo-400'
export const badgeMuted = 'bg-gray-800 text-gray-500'

// ── Tab bar ─────────────────────────────────────────────────────

export const tabBar = 'flex gap-1 border-b border-gray-800 overflow-x-auto'

export const tabBtn = (active: boolean) =>
  `flex items-center gap-1.5 px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px shrink-0 whitespace-nowrap ${
    active
      ? 'text-indigo-400 border-indigo-400'
      : 'text-gray-500 border-transparent hover:text-gray-300 hover:border-gray-700'
  }`

// ── Page layout ─────────────────────────────────────────────────

export const pageContainer = 'p-4 md:p-6'
export const pageTitle = 'text-xl font-semibold text-white'
export const pageSubtitle = 'text-sm text-gray-500 mt-1'

// ── Section headers ─────────────────────────────────────────────

export const sectionHeader =
  'flex items-center gap-2 text-sm font-medium text-gray-300'

export const sectionLabel =
  'text-[11px] font-medium text-gray-600 uppercase tracking-widest'

// ── Status colors ───────────────────────────────────────────────

export const STATE_STYLES: Record<string, { bg: string; text: string; dot: string; border: string }> = {
  intake:      { bg: 'bg-violet-500/10', text: 'text-violet-400', dot: 'bg-violet-500', border: 'border-l-violet-500' },
  planning:    { bg: 'bg-blue-500/10',   text: 'text-blue-400',   dot: 'bg-blue-500',   border: 'border-l-blue-500' },
  plan_ready:  { bg: 'bg-cyan-500/10',   text: 'text-cyan-400',   dot: 'bg-cyan-500',   border: 'border-l-cyan-500' },
  scheduled:   { bg: 'bg-indigo-500/10', text: 'text-indigo-400', dot: 'bg-indigo-500', border: 'border-l-indigo-500' },
  in_progress: { bg: 'bg-amber-500/10',  text: 'text-amber-400',  dot: 'bg-amber-500',  border: 'border-l-amber-500' },
  testing:     { bg: 'bg-teal-500/10',   text: 'text-teal-400',   dot: 'bg-teal-500',   border: 'border-l-teal-500' },
  review:      { bg: 'bg-orange-500/10', text: 'text-orange-400', dot: 'bg-orange-500', border: 'border-l-orange-500' },
  completed:   { bg: 'bg-emerald-500/10', text: 'text-emerald-400', dot: 'bg-emerald-500', border: 'border-l-emerald-500' },
  failed:      { bg: 'bg-red-500/10',    text: 'text-red-400',    dot: 'bg-red-500',    border: 'border-l-red-500' },
  cancelled:   { bg: 'bg-gray-500/10',   text: 'text-gray-500',   dot: 'bg-gray-600',   border: 'border-l-gray-600' },
}

export const PRIORITY_STYLES: Record<string, { text: string; dot: string; label: string }> = {
  critical: { text: 'text-red-400',    dot: 'bg-red-400',    label: 'Critical' },
  high:     { text: 'text-orange-400', dot: 'bg-orange-400', label: 'High' },
  medium:   { text: 'text-gray-400',   dot: 'bg-gray-500',   label: 'Medium' },
  low:      { text: 'text-gray-600',   dot: 'bg-gray-600',   label: 'Low' },
}
