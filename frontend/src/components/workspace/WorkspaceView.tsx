import { useEffect, useState, useCallback, useRef } from 'react'
import Editor from '@monaco-editor/react'
import { X, PanelBottomClose, PanelBottomOpen, PanelLeftClose, PanelLeftOpen, Loader2, Diff, FileCode, GitBranch } from 'lucide-react'
import { todos } from '../../services/api'
import type { FileTreeNode, GitStatus } from '../../types'
import FileTree from './FileTree'
import GitPanel from './GitPanel'
import DiffViewer from '../DiffViewer'

interface OpenTab {
  path: string
  content: string
  savedContent: string
  language: string
  dirty: boolean
  /** When set, this tab is showing a diff instead of the editor */
  diffMode?: boolean
  diffContent?: string
  diffStats?: string
  diffStaged?: boolean
  diffLoading?: boolean
  diffError?: string
}

interface WorkspaceRepo {
  name: string
  label: string
}

interface Props {
  todoId: string
}

export default function WorkspaceView({ todoId }: Props) {
  const [tree, setTree] = useState<FileTreeNode[]>([])
  const [tabs, setTabs] = useState<OpenTab[]>([])
  const [activeTab, setActiveTab] = useState<string | null>(null)
  const [gitStatus, setGitStatus] = useState<GitStatus>({ branch: '', files: [], clean: true })
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [gitPanelCollapsed, setGitPanelCollapsed] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const editorRef = useRef<unknown>(null)

  // Multi-repo state
  const [availableRepos, setAvailableRepos] = useState<WorkspaceRepo[]>([])
  const [selectedRepo, setSelectedRepo] = useState<string | undefined>(undefined)

  // Full diff state for "all changes" view
  const [fullDiff, setFullDiff] = useState<{ diff: string; stats: string } | null>(null)
  const [fullDiffLoading, setFullDiffLoading] = useState(false)
  const [showFullDiff, setShowFullDiff] = useState(false)

  const loadRepos = useCallback(async () => {
    try {
      const repos = await todos.workspace.repos(todoId)
      setAvailableRepos(repos)
    } catch {
      // Single-repo fallback
      setAvailableRepos([{ name: 'main', label: 'Main Repository' }])
    }
  }, [todoId])

  const loadTree = useCallback(async () => {
    try {
      const t = await todos.workspace.tree(todoId, selectedRepo)
      setTree(t)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load file tree')
    }
  }, [todoId, selectedRepo])

  const loadGitStatus = useCallback(async () => {
    try {
      const s = await todos.workspace.gitStatus(todoId, selectedRepo)
      setGitStatus(s)
    } catch {
      // Ignore git status errors
    }
  }, [todoId, selectedRepo])

  useEffect(() => {
    loadRepos()
  }, [loadRepos])

  useEffect(() => {
    Promise.all([loadTree(), loadGitStatus()]).finally(() => setLoading(false))
  }, [loadTree, loadGitStatus])

  // Reset workspace state when switching repos
  const handleRepoChange = useCallback((repoName: string) => {
    const repo = repoName === 'main' ? undefined : repoName
    setSelectedRepo(repo)
    setTabs([])
    setActiveTab(null)
    setFullDiff(null)
    setShowFullDiff(false)
    setLoading(true)
  }, [])

  const handleFileSelect = useCallback(async (path: string) => {
    // Check if tab already open (in editor mode)
    const existing = tabs.find(t => t.path === path && !t.diffMode)
    if (existing) {
      setActiveTab(path)
      setShowFullDiff(false)
      return
    }

    try {
      const file = await todos.workspace.file(todoId, path, selectedRepo)
      if (file.binary) {
        setError('Binary files cannot be opened in the editor')
        return
      }
      const newTab: OpenTab = {
        path: file.path,
        content: file.content,
        savedContent: file.content,
        language: file.language,
        dirty: false,
      }
      // Remove any existing diff tab for this file if present
      setTabs(prev => [...prev.filter(t => !(t.path === path && t.diffMode)), newTab])
      setActiveTab(path)
      setShowFullDiff(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to open file')
    }
  }, [tabs, todoId, selectedRepo])

  const handleDiffClick = useCallback(async (path: string, staged: boolean) => {
    // Create a diff tab identifier
    const diffTabId = `diff:${path}`

    // Check if diff tab already open
    const existing = tabs.find(t => t.path === diffTabId)
    if (existing) {
      setActiveTab(diffTabId)
      setShowFullDiff(false)
      return
    }

    // Create a diff tab with loading state
    const newTab: OpenTab = {
      path: diffTabId,
      content: '',
      savedContent: '',
      language: '',
      dirty: false,
      diffMode: true,
      diffLoading: true,
      diffStaged: staged,
    }
    setTabs(prev => [...prev, newTab])
    setActiveTab(diffTabId)
    setShowFullDiff(false)

    // Fetch the diff
    try {
      const result = await todos.workspace.gitDiff(todoId, staged, path, selectedRepo)
      setTabs(prev => prev.map(t =>
        t.path === diffTabId
          ? { ...t, diffLoading: false, diffContent: result.diff, diffStats: result.stats }
          : t
      ))
    } catch (e) {
      setTabs(prev => prev.map(t =>
        t.path === diffTabId
          ? { ...t, diffLoading: false, diffError: e instanceof Error ? e.message : 'Failed to load diff' }
          : t
      ))
    }
  }, [tabs, todoId, selectedRepo])

  const handleCloseTab = useCallback((path: string, e?: React.MouseEvent) => {
    e?.stopPropagation()
    setTabs(prev => {
      const filtered = prev.filter(t => t.path !== path)
      if (activeTab === path) {
        const idx = prev.findIndex(t => t.path === path)
        const next = filtered[Math.min(idx, filtered.length - 1)]
        setActiveTab(next?.path || null)
      }
      return filtered
    })
  }, [activeTab])

  const handleEditorChange = useCallback((value: string | undefined) => {
    if (!activeTab || value === undefined) return
    setTabs(prev => prev.map(t =>
      t.path === activeTab
        ? { ...t, content: value, dirty: value !== t.savedContent }
        : t
    ))
  }, [activeTab])

  const handleSave = useCallback(async () => {
    const tab = tabs.find(t => t.path === activeTab)
    if (!tab || !tab.dirty || tab.diffMode) return

    try {
      await todos.workspace.saveFile(todoId, tab.path, tab.content, selectedRepo)
      setTabs(prev => prev.map(t =>
        t.path === tab.path
          ? { ...t, savedContent: t.content, dirty: false }
          : t
      ))
      loadGitStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save file')
    }
  }, [activeTab, tabs, todoId, loadGitStatus, selectedRepo])

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's') {
        e.preventDefault()
        handleSave()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [handleSave])

  const handleEditorMount = useCallback((editor: unknown) => {
    editorRef.current = editor
  }, [])

  const handleGitRefresh = useCallback(() => {
    loadGitStatus()
    loadTree()
  }, [loadGitStatus, loadTree])

  const handleGitFileClick = useCallback((path: string) => {
    handleFileSelect(path)
  }, [handleFileSelect])

  const handleLoadFullDiff = useCallback(async () => {
    setShowFullDiff(true)
    setActiveTab(null)
    if (!fullDiff) {
      setFullDiffLoading(true)
      try {
        const result = await todos.workspace.gitDiff(todoId, undefined, undefined, selectedRepo)
        setFullDiff(result)
      } catch {
        setFullDiff({ diff: '', stats: '' })
      } finally {
        setFullDiffLoading(false)
      }
    }
  }, [todoId, fullDiff, selectedRepo])

  const modifiedPaths = new Set(gitStatus.files.map(f => f.path))
  const currentTab = tabs.find(t => t.path === activeTab)
  const fileName = (path: string) => {
    if (path.startsWith('diff:')) {
      return path.replace('diff:', '').split('/').pop() || path
    }
    return path.split('/').pop() || path
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="w-5 h-5 text-gray-600 animate-spin" />
      </div>
    )
  }

  if (error && tree.length === 0) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <p className="text-sm text-red-400">{error}</p>
          <p className="text-xs text-gray-600 mt-1">Make sure the task has been executed and a workspace exists.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full overflow-hidden bg-gray-950">
      {/* File tree sidebar */}
      <div
        className={`border-r border-gray-800 flex flex-col shrink-0 transition-[width] duration-200 overflow-hidden ${
          sidebarCollapsed ? 'w-0' : 'w-56'
        }`}
      >
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-gray-800">
          <span className="text-[11px] text-gray-600 uppercase tracking-wider font-medium">Explorer</span>
          <button
            onClick={() => setSidebarCollapsed(true)}
            className="text-gray-600 hover:text-gray-400 transition-colors"
          >
            <PanelLeftClose className="w-3.5 h-3.5" />
          </button>
        </div>
        {/* Repo selector — only shown when multiple repos available */}
        {availableRepos.length > 1 && (
          <div className="px-2 py-1.5 border-b border-gray-800">
            <div className="flex items-center gap-1.5 mb-1">
              <GitBranch className="w-3 h-3 text-gray-600" />
              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Repository</span>
            </div>
            <select
              value={selectedRepo || 'main'}
              onChange={e => handleRepoChange(e.target.value)}
              className="w-full px-2 py-1 bg-gray-900 border border-gray-800 rounded text-xs text-white focus:outline-none focus:border-indigo-500 transition-colors"
            >
              {availableRepos.map(r => (
                <option key={r.name} value={r.name}>{r.label}</option>
              ))}
            </select>
          </div>
        )}
        <FileTree
          tree={tree}
          activeFile={activeTab && !activeTab.startsWith('diff:') ? activeTab : null}
          modifiedPaths={modifiedPaths}
          onFileSelect={handleFileSelect}
        />
      </div>

      {/* Main editor area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Tab bar */}
        <div className="flex items-center border-b border-gray-800 bg-gray-950 overflow-x-auto">
          {sidebarCollapsed && (
            <button
              onClick={() => setSidebarCollapsed(false)}
              className="px-2 py-1.5 text-gray-600 hover:text-gray-400 transition-colors shrink-0 border-r border-gray-800"
            >
              <PanelLeftOpen className="w-3.5 h-3.5" />
            </button>
          )}
          {tabs.map(tab => (
            <button
              key={tab.path}
              onClick={() => { setActiveTab(tab.path); setShowFullDiff(false) }}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-xs border-r border-gray-800 shrink-0 transition-colors ${
                activeTab === tab.path && !showFullDiff
                  ? 'bg-gray-900 text-white border-b-2 border-b-indigo-500 -mb-px'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {tab.diffMode ? (
                <Diff className="w-3 h-3 text-amber-400 shrink-0" />
              ) : null}
              <span className="truncate max-w-[120px]">
                {tab.dirty && <span className="text-amber-400 mr-0.5">*</span>}
                {fileName(tab.path)}
              </span>
              <span
                onClick={(e) => handleCloseTab(tab.path, e)}
                className="hover:bg-gray-700 rounded p-0.5 transition-colors"
              >
                <X className="w-3 h-3" />
              </span>
            </button>
          ))}
          {/* All Changes diff tab */}
          {!gitStatus.clean && (
            <button
              onClick={handleLoadFullDiff}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-xs border-r border-gray-800 shrink-0 transition-colors ${
                showFullDiff
                  ? 'bg-gray-900 text-white border-b-2 border-b-indigo-500 -mb-px'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
              title="View all changes"
            >
              <Diff className="w-3 h-3 text-amber-400" />
              <span className="truncate">All Changes</span>
            </button>
          )}
          <div className="flex-1" />
          <button
            onClick={() => setGitPanelCollapsed(c => !c)}
            className="px-2 py-1.5 text-gray-600 hover:text-gray-400 transition-colors shrink-0"
            title={gitPanelCollapsed ? 'Show Source Control' : 'Hide Source Control'}
          >
            {gitPanelCollapsed ? (
              <PanelBottomOpen className="w-3.5 h-3.5" />
            ) : (
              <PanelBottomClose className="w-3.5 h-3.5" />
            )}
          </button>
        </div>

        {/* Editor + git panel */}
        <div className="flex-1 flex flex-col min-h-0">
          {/* Editor / Diff content */}
          <div className="flex-1 min-h-0">
            {showFullDiff ? (
              /* Full diff view */
              <div className="h-full overflow-y-auto bg-gray-950 p-4">
                <div className="max-w-5xl mx-auto">
                  <div className="flex items-center gap-2 mb-3">
                    <Diff className="w-4 h-4 text-amber-400" />
                    <h2 className="text-sm font-medium text-white">All Changes</h2>
                    <span className="text-[11px] text-gray-500">
                      {gitStatus.branch && `on ${gitStatus.branch}`}
                    </span>
                  </div>
                  {fullDiffLoading ? (
                    <div className="flex items-center gap-2 py-8 justify-center text-gray-600">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      <span className="text-sm">Loading diff...</span>
                    </div>
                  ) : fullDiff && fullDiff.diff ? (
                    <DiffViewer
                      diff={fullDiff.diff}
                      stats={fullDiff.stats}
                      files={gitStatus.files.map(f => ({ status: f.status, path: f.path }))}
                      maxHeight="max-h-full"
                    />
                  ) : (
                    <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
                      No diff available
                    </div>
                  )}
                </div>
              </div>
            ) : currentTab?.diffMode ? (
              /* Per-file diff view */
              <div className="h-full overflow-y-auto bg-gray-950 p-4">
                <div className="max-w-5xl mx-auto">
                  <div className="flex items-center gap-2 mb-3">
                    <Diff className="w-4 h-4 text-amber-400" />
                    <h2 className="text-sm font-medium text-white">
                      {currentTab.path.replace('diff:', '')}
                    </h2>
                    {currentTab.diffStaged && (
                      <span className="px-2 py-0.5 bg-emerald-500/10 border border-emerald-500/20 rounded text-[10px] text-emerald-400 font-medium">
                        staged
                      </span>
                    )}
                    <button
                      onClick={() => handleFileSelect(currentTab.path.replace('diff:', ''))}
                      className="ml-auto flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-300 transition-colors"
                      title="Open in editor"
                    >
                      <FileCode className="w-3 h-3" />
                      Open in Editor
                    </button>
                  </div>
                  {currentTab.diffLoading ? (
                    <div className="flex items-center gap-2 py-8 justify-center text-gray-600">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      <span className="text-sm">Loading diff...</span>
                    </div>
                  ) : currentTab.diffError ? (
                    <div className="px-4 py-2.5 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-sm">
                      {currentTab.diffError}
                    </div>
                  ) : currentTab.diffContent ? (
                    <DiffViewer
                      diff={currentTab.diffContent}
                      stats={currentTab.diffStats}
                      maxHeight="max-h-full"
                    />
                  ) : (
                    <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
                      No diff available for this file
                    </div>
                  )}
                </div>
              </div>
            ) : currentTab ? (
              <Editor
                height="100%"
                language={currentTab.language}
                value={currentTab.content}
                theme="vs-dark"
                onChange={handleEditorChange}
                onMount={handleEditorMount}
                options={{
                  fontSize: 13,
                  lineHeight: 20,
                  minimap: { enabled: false },
                  scrollBeyondLastLine: false,
                  automaticLayout: true,
                  padding: { top: 8 },
                  renderLineHighlight: 'line',
                  cursorBlinking: 'smooth',
                  smoothScrolling: true,
                  wordWrap: 'off',
                  tabSize: 2,
                  bracketPairColorization: { enabled: true },
                }}
              />
            ) : (
              <div className="flex items-center justify-center h-full text-gray-700">
                <div className="text-center">
                  <p className="text-sm">Select a file to open</p>
                  <p className="text-xs mt-1 text-gray-800">Browse files in the explorer sidebar</p>
                </div>
              </div>
            )}
          </div>

          {/* Git panel */}
          <GitPanel
            todoId={todoId}
            gitStatus={gitStatus}
            onRefresh={handleGitRefresh}
            onFileClick={handleGitFileClick}
            onDiffClick={handleDiffClick}
            collapsed={gitPanelCollapsed}
            onToggle={() => setGitPanelCollapsed(c => !c)}
            repo={selectedRepo}
          />
        </div>
      </div>

      {/* Error toast - auto-dismiss */}
      {error && tree.length > 0 && (
        <div className="absolute bottom-4 right-4 max-w-sm px-4 py-2.5 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-xs animate-in fade-in z-50">
          {error}
          <button
            onClick={() => setError('')}
            className="ml-2 text-red-500 hover:text-red-400"
          >
            <X className="w-3 h-3 inline" />
          </button>
        </div>
      )}
    </div>
  )
}
