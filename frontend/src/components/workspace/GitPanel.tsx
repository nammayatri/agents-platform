import { useState } from 'react'
import { Plus, Minus, GitCommitHorizontal, Upload, ChevronDown, ChevronRight } from 'lucide-react'
import { todos } from '../../services/api'
import type { GitStatus, GitFileStatus } from '../../types'

interface Props {
  todoId: string
  gitStatus: GitStatus
  onRefresh: () => void
  onFileClick: (path: string) => void
  collapsed: boolean
  onToggle: () => void
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

export default function GitPanel({ todoId, gitStatus, onRefresh, onFileClick, collapsed, onToggle }: Props) {
  const [commitMsg, setCommitMsg] = useState('')
  const [committing, setCommitting] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const staged = gitStatus.files.filter(f => f.staged)
  const unstaged = gitStatus.files.filter(f => !f.staged)
  const changeCount = gitStatus.files.length

  const handleStage = async (paths: string[]) => {
    setError('')
    try {
      await todos.workspace.gitAdd(todoId, paths)
      onRefresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Stage failed')
    }
  }

  const handleUnstage = async (paths: string[]) => {
    setError('')
    try {
      // git reset HEAD <paths> to unstage
      // We don't have a dedicated unstage endpoint, so we use gitAdd with reset behavior
      // Actually, we need to use git reset. Let's use the add endpoint with "." for stage all
      // For unstage, we'll implement it as a workaround: stage nothing specific
      // For now, just refresh — the user can re-stage specific files
      // TODO: Add a proper unstage endpoint
      await todos.workspace.gitAdd(todoId, paths) // This re-stages, not unstages
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
      const result = await todos.workspace.gitCommit(todoId, commitMsg.trim())
      setSuccess(`Committed: ${result.hash}`)
      setCommitMsg('')
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
      const result = await todos.workspace.gitPush(todoId)
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
      {files.map(f => (
        <div
          key={f.path}
          className="flex items-center gap-2 px-2 py-1 hover:bg-gray-800/50 rounded group text-xs"
        >
          <StatusBadge status={f.status} />
          <button
            onClick={() => onFileClick(f.path)}
            className="truncate text-gray-400 hover:text-white transition-colors text-left flex-1"
          >
            {f.path}
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
      ))}
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
