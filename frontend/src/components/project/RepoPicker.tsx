import { useState, useRef, useEffect, useCallback } from 'react'
import type { GitProviderConfig, RepoInfo, ProviderRepos } from '../../types'
import { gitProviders } from '../../services/api'
import { inputClass } from '../../styles/classes'

interface RepoPickerProps {
  value: string
  onChange: (repoUrl: string) => void
  onRepoSelect?: (repo: RepoInfo, providerId: string) => void
  gitProviderList: GitProviderConfig[]
  placeholder?: string
}

// Module-level cache: survives component unmounts, cleared on page refresh
let _repoCache: ProviderRepos[] | null = null
let _repoCachePromise: Promise<ProviderRepos[]> | null = null

function fetchReposWithCache(): Promise<ProviderRepos[]> {
  if (_repoCache) return Promise.resolve(_repoCache)
  if (_repoCachePromise) return _repoCachePromise
  _repoCachePromise = gitProviders.listRepos().then((data) => {
    _repoCache = data
    _repoCachePromise = null
    return data
  }).catch((err) => {
    _repoCachePromise = null
    throw err
  })
  return _repoCachePromise
}

export default function RepoPicker({ value, onChange, onRepoSelect, gitProviderList, placeholder }: RepoPickerProps) {
  const [open, setOpen] = useState(false)
  const [providerRepos, setProviderRepos] = useState<ProviderRepos[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [highlightIdx, setHighlightIdx] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const hasProviders = gitProviderList.length > 0

  // Load repos on first open
  const loadRepos = useCallback(() => {
    if (!hasProviders || _repoCache) {
      if (_repoCache) setProviderRepos(_repoCache)
      return
    }
    setLoading(true)
    setError('')
    fetchReposWithCache()
      .then((data) => setProviderRepos(data))
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load repos'))
      .finally(() => setLoading(false))
  }, [hasProviders])

  // Build flat list of selectable items for keyboard nav
  const filter = value.trim().toLowerCase()
  const flatItems: { repo: RepoInfo; providerId: string }[] = []
  for (const pr of providerRepos) {
    for (const repo of pr.repos) {
      if (!filter || repo.full_name.toLowerCase().includes(filter) || repo.clone_url.toLowerCase().includes(filter)) {
        flatItems.push({ repo, providerId: pr.provider_id })
      }
    }
  }

  // Show manual entry option when typed text doesn't match any repo exactly
  const hasExactMatch = flatItems.some((item) => item.repo.clone_url === value)
  const showManual = value.trim().length > 0 && !hasExactMatch

  // Close on click outside
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    if (open) {
      document.addEventListener('mousedown', handleClickOutside)
      return () => document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [open])

  // Scroll highlighted item into view
  useEffect(() => {
    if (highlightIdx >= 0 && listRef.current) {
      const items = listRef.current.querySelectorAll('[data-repo-item]')
      items[highlightIdx]?.scrollIntoView({ block: 'nearest' })
    }
  }, [highlightIdx])

  const handleOpen = () => {
    if (!open) {
      setOpen(true)
      setHighlightIdx(-1)
      loadRepos()
    }
  }

  const selectRepo = (repo: RepoInfo, providerId: string) => {
    onChange(repo.clone_url)
    onRepoSelect?.(repo, providerId)
    setOpen(false)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (!open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        handleOpen()
        e.preventDefault()
      }
      return
    }

    const totalItems = flatItems.length + (showManual ? 1 : 0)
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlightIdx((prev) => (prev + 1) % totalItems)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlightIdx((prev) => (prev <= 0 ? totalItems - 1 : prev - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (showManual && highlightIdx === 0) {
        // Manual entry selected - keep the typed value
        setOpen(false)
      } else {
        const idx = showManual ? highlightIdx - 1 : highlightIdx
        if (idx >= 0 && idx < flatItems.length) {
          selectRepo(flatItems[idx].repo, flatItems[idx].providerId)
        }
      }
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  // If no providers, render plain input
  if (!hasProviders) {
    return (
      <input
        className={inputClass}
        placeholder={placeholder || 'https://github.com/org/repo'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }

  // Build grouped display
  const filteredGroups: { provider: ProviderRepos; repos: RepoInfo[] }[] = []
  for (const pr of providerRepos) {
    const filtered = pr.repos.filter((r) =>
      !filter || r.full_name.toLowerCase().includes(filter) || r.clone_url.toLowerCase().includes(filter)
    )
    if (filtered.length > 0 || pr.error) {
      filteredGroups.push({ provider: pr, repos: filtered })
    }
  }

  // Pre-compute flat index for each repo to support keyboard highlight
  const repoFlatIndex = new Map<string, number>()
  let idx = showManual ? 1 : 0
  for (const group of filteredGroups) {
    for (const repo of group.repos) {
      repoFlatIndex.set(group.provider.provider_id + ':' + repo.clone_url, idx++)
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative">
        <input
          ref={inputRef}
          className={inputClass + ' pr-8'}
          placeholder={placeholder || 'Search repos or paste URL...'}
          value={value}
          onChange={(e) => { onChange(e.target.value); if (!open) handleOpen() }}
          onFocus={handleOpen}
          onKeyDown={handleKeyDown}
        />
        <button
          type="button"
          tabIndex={-1}
          onClick={() => { if (open) setOpen(false); else { handleOpen(); inputRef.current?.focus() } }}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-400 transition-colors"
        >
          <svg className={`w-4 h-4 transition-transform ${open ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </button>
      </div>

      {open && (
        <div ref={listRef} className="absolute z-50 mt-1 w-full max-h-64 overflow-y-auto bg-gray-950 border border-gray-800 rounded-lg shadow-xl">
          {loading && (
            <div className="px-3 py-3 text-sm text-gray-500 flex items-center gap-2">
              <svg className="w-4 h-4 animate-spin text-gray-600" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Loading repositories...
            </div>
          )}

          {error && (
            <div className="px-3 py-2 text-xs text-red-400">{error}</div>
          )}

          {showManual && (
            <button
              type="button"
              data-repo-item
              onClick={() => setOpen(false)}
              className={`w-full text-left px-3 py-2 text-sm border-b border-gray-800 transition-colors ${
                highlightIdx === 0 ? 'bg-indigo-500/10 text-indigo-300' : 'text-indigo-400 hover:bg-gray-900/50'
              }`}
            >
              Use: <span className="font-mono text-xs">{value}</span>
            </button>
          )}

          {!loading && filteredGroups.map((group) => {
            return (
              <div key={group.provider.provider_id}>
                <div className="px-3 py-1.5 text-[11px] text-gray-600 uppercase tracking-wider bg-gray-900/50 sticky top-0 flex items-center justify-between">
                  <span>{group.provider.provider_name} ({group.provider.provider_type})</span>
                  {group.provider.error && <span className="text-red-400 normal-case">{group.provider.error}</span>}
                </div>
                {group.repos.map((repo) => {
                  const currentIdx = repoFlatIndex.get(group.provider.provider_id + ':' + repo.clone_url) ?? -1
                  const isHighlighted = currentIdx === highlightIdx
                  return (
                    <button
                      type="button"
                      key={repo.clone_url}
                      data-repo-item
                      onClick={() => selectRepo(repo, group.provider.provider_id)}
                      className={`w-full text-left px-3 py-2 text-sm transition-colors flex items-center justify-between gap-2 ${
                        isHighlighted ? 'bg-indigo-500/10 text-white' : 'text-gray-300 hover:bg-gray-900/50'
                      }`}
                    >
                      <span className="truncate font-mono text-xs">{repo.full_name}</span>
                      <span className="flex items-center gap-1.5 shrink-0">
                        {repo.private && (
                          <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500">private</span>
                        )}
                        <span className="text-[11px] text-gray-600">{repo.default_branch}</span>
                      </span>
                    </button>
                  )
                })}
              </div>
            )
          })}

          {!loading && !error && flatItems.length === 0 && !showManual && (
            <div className="px-3 py-3 text-sm text-gray-600">
              {filter ? 'No matching repos found' : 'No repositories found'}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
