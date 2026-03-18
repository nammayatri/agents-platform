import { useState, useCallback } from 'react'
import { Plus, Minus, GitCommitHorizontal, Upload, ChevronDown, ChevronRight, Diff, Loader2 } from 'lucide-react'
import { todos } from '../../services/api'
import type { GitStatus, GitFileStatus } from '../../types'
import DiffViewer from '../DiffViewer'

interface Props {
  todoId: string
  gitStatus: GitStatus
  onRefresh: () => void
  onFileClick: (path: string) => void
  onDiffClick: (path: string, staged: boolean) => void
  collapsed: boolean
  onToggle: () => void
  repo?: string
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    'M': 'text-amber-400',
    'A': 'text-green-400',
    'D': 'text-red-400',
    'R': 'text-blue-400',
    '??': 'text-gray-500',
    'U': 'text-orange-400',
  }
  return (
    <span className={`font-mono text-[11px] font-medium w-5 shrink-0 ${colors[status] || 'text-gray-500'}`}>
      {status}
    </span>
  )
}

interface InlineDiffState {
  loading: boolean
  diff: string
  stats: string
  error: string
}

export default function GitPanel({ todoId, gitStatus, onRefresh, onFileClick, onDiffClick, collapsed, onToggle, repo }: Props) {
  const [commitMsg, setCommitMsg] = useState('')
  const [committing, setCommitting] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [expandedDiffs, setExpandedDiffs] = useState<Set<string>>(new Set())
  const [diffCache, setDiffCache] = useState<Record<string, InlineDiffState>>({})

  const staged = gitStatus.files.filter(f => f.staged)
  const unstaged = gitStatus.files.filter(f => !f.staged)
  const changeCount = gitStatus.files.length

  const toggleInlineDiff = useCallback(async (path: string, isStagedFile: boolean) => {
    const key = `${isStagedFile ? 'staged' : 'unstaged'}:${path}`
    setExpandedDiffs(prev => {
      const next = new Set(prev)
      if (next.has(key)) {
        next.delete(key)
      } else {
        next.add(key)
      }
      return next
    })

    // Fetch diff if not cached
    if (!diffCache[key] || diffCache[key].error) {
      setDiffCache(prev => ({ ...prev, [key]: { loading: true, diff: '', stats: '', error: '' } }))
      try {
        const result = await todos.workspace.gitDiff(todoId, isStagedFile, path, repo)
        setDiffCache(prev => ({
          ...prev,
          [key]: { loading: false, diff: result.diff, stats: result.stats, error: '' },
        }))
      } catch (e) {
        setDiffCache(prev => ({
          ...prev,
          [key]: { loading: false, diff: '', stats: '', error: e instanceof Error ? e.message : 'Failed to load diff' },
        }))
      }
    }
  }, [todoId, diffCache, repo])

  const handleStage = async (paths: string[]) => {
    setError('')
    try {
      await todos.workspace.gitAdd(todoId, paths, repo)
      onRefresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Stage failed')
    }
  }

  const handleUnstage = async (paths: string[]) => {
    setError('')
    try {
      await todos.workspace.gitAdd(todoId, paths, repo)
      onRefresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unstage failed')
    }
  }

  const handleStageAll = async () => {
    await handleStage(['.'])
  }

  const handleCommit = async () => {
    if (!commitMsg.trim()) return
    setError('')
    setSuccess('')
    setCommitting(true)
    try {
      const result = await todos.workspace.gitCommit(todoId, commitMsg.trim(), repo)
      setSuccess(`Committed: ${result.hash}`)
      setCommitMsg('')
      // Clear diff cache on commit
      setDiffCache({})
      setExpandedDiffs(new Set())
      onRefresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Commit failed')
    } finally {
      setCommitting(false)
    }
  }

  const handlePush = async () => {
    setError('')
    setSuccess('')
    setPushing(true)
    try {
      const result = await todos.workspace.gitPush(todoId, repo)
      setSuccess(`Pushed to ${result.branch}`)
      onRefresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Push failed')
    } finally {
      setPushing(false)
    }
  }

  const renderFileList = (files: GitFileStatus[], showStage: boolean) => (
    <div className="space-y-px">
      {files.map(f => {
        const key = `${showStage ? 'unstaged' : 'staged'}:${f.path}`
        const isExpanded = expandedDiffs.has(key)
        const cachedDiff = diffCache[key]

        return (
          <div key={f.path}>
            <div className="flex items-center gap-2 px-2 py-1 hover:bg-gray-800/50 rounded group text-xs">
              <StatusBadge status={f.status} />
              <button
                onClick={() => onFileClick(f.path)}
                className="truncate text-gray-400 hover:text-white transition-colors text-left flex-1"
              >
                {f.path}
              </button>
              <button
                onClick={() => onDiffClick(f.path, !showStage)}
                className="opacity-0 group-hover:opacity-100 text-indigo-400 hover:text-indigo-300 transition-all"
                title="View diff in editor"
              >
                <Diff className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={() => toggleInlineDiff(f.path, !showStage)}
                className={`transition-all ${
                  isExpanded
                    ? 'text-indigo-400 opacity-100'
                    : 'opacity-0 group-hover:opacity-100 text-gray-500 hover:text-gray-300'
                }`}
                title={isExpanded ? 'Collapse diff' : 'Expand diff'}
              >
                {isExpanded ? (
                  <ChevronDown className="w-3.5 h-3.5" />
                ) : (
                  <ChevronRight className="w-3.5 h-3.5" />
                )}
              </button>
              {showStage ? (
                <button
                  onClick={() => handleStage([f.path])}
                  className="opacity-0 group-hover:opacity-100 text-green-500 hover:text-green-400 transition-all"
                  title="Stage"
                >
                  <Plus className="w-3.5 h-3.5" />
                </button>
              ) : (
                <button
                  onClick={() => handleUnstage([f.path])}
                  className="opacity-0 group-hover:opacity-100 text-red-500 hover:text-red-400 transition-all"
                  title="Unstage"
                >
                  <Minus className="w-3.5 h-3.5" />
                </button>
              )}
            </div>

            {/* Inline diff */}
            {isExpanded && (
              <div className="mx-2 mb-1">
                {cachedDiff?.loading && (
                  <div className="flex items-center gap-2 px-3 py-2 text-[11px] text-gray-600">
                    <Loader2 className="w-3 h-3 animate-spin" />
                    Loading diff...
                  </div>
                )}
                {cachedDiff?.error && (
                  <div className="px-3 py-2 text-[11px] text-red-400 bg-red-500/5 rounded">
                    {cachedDiff.error}
                  </div>
                )}
                {cachedDiff && !cachedDiff.loading && !cachedDiff.error && cachedDiff.diff && (
                  <DiffViewer
                    diff={cachedDiff.diff}
                    stats={cachedDiff.stats}
                    maxHeight="max-h-[300px]"
                  />
                )}
                {cachedDiff && !cachedDiff.loading && !cachedDiff.error && !cachedDiff.diff && (
                  <div className="px-3 py-2 text-[11px] text-gray-600 text-center border border-dashed border-gray-800 rounded">
                    No diff available
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )

  return (
    <div className="border-t border-gray-800 bg-gray-950 flex flex-col">
      {/* Header */}
      <button
        onClick={onToggle}
        className="flex items-center gap-2 px-3 py-1.5 text-xs text-gray-500 hover:text-gray-300 transition-colors"
      >
        {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <span className="uppercase tracking-wider font-medium">Source Control</span>
        {changeCount > 0 && (
          <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-400">
            {changeCount}
          </span>
        )}
      </button>

      {!collapsed && (
        <div className="flex-1 overflow-y-auto px-1 pb-2">
          {/* Staged Changes */}
          {staged.length > 0 && (
            <div className="mb-2">
              <div className="px-2 py-1 text-[11px] text-gray-600 uppercase tracking-wider font-medium">
                Staged Changes
              </div>
              {renderFileList(staged, false)}
            </div>
          )}

          {/* Unstaged Changes */}
          {unstaged.length > 0 && (
            <div className="mb-2">
              <div className="flex items-center justify-between px-2 py-1">
                <span className="text-[11px] text-gray-600 uppercase tracking-wider font-medium">
                  Changes
                </span>
                <button
                  onClick={handleStageAll}
                  className="text-[10px] text-green-500 hover:text-green-400 transition-colors"
                  title="Stage All"
                >
                  Stage All
                </button>
              </div>
              {renderFileList(unstaged, true)}
            </div>
          )}

          {gitStatus.clean && (
            <div className="px-3 py-4 text-xs text-gray-600 text-center">
              Working tree clean
            </div>
          )}

          {/* Commit area */}
          <div className="px-2 pt-1 space-y-1.5">
            <input
              type="text"
              value={commitMsg}
              onChange={e => setCommitMsg(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleCommit() } }}
              placeholder="Commit message..."
              className="w-full px-2.5 py-1.5 bg-gray-900 border border-gray-800 rounded text-xs text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors"
              disabled={committing}
            />
            <div className="flex gap-1.5">
              <button
                onClick={handleCommit}
                disabled={committing || !commitMsg.trim() || staged.length === 0}
                className="flex items-center gap-1 px-2.5 py-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 rounded text-xs text-white font-medium transition-colors"
              >
                <GitCommitHorizontal className="w-3 h-3" />
                {committing ? 'Committing...' : 'Commit'}
              </button>
              <button
                onClick={handlePush}
                disabled={pushing}
                className="flex items-center gap-1 px-2.5 py-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded text-xs text-gray-300 transition-colors"
              >
                <Upload className="w-3 h-3" />
                {pushing ? 'Pushing...' : 'Push'}
              </button>
            </div>
          </div>

          {/* Feedback */}
          {error && (
            <div className="mx-2 mt-1.5 px-2.5 py-1.5 bg-red-500/10 border border-red-500/20 rounded text-[11px] text-red-400">
              {error}
            </div>
          )}
          {success && (
            <div className="mx-2 mt-1.5 px-2.5 py-1.5 bg-emerald-500/10 border border-emerald-500/20 rounded text-[11px] text-emerald-400">
              {success}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
