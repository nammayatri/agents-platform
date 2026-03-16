import { useEffect, useState, useCallback, useRef } from 'react'
import Editor from '@monaco-editor/react'
import { X, PanelBottomClose, PanelBottomOpen, PanelLeftClose, PanelLeftOpen, Loader2 } from 'lucide-react'
import { todos } from '../../services/api'
import type { FileTreeNode, GitStatus } from '../../types'
import FileTree from './FileTree'
import GitPanel from './GitPanel'

interface OpenTab {
  path: string
  content: string
  savedContent: string
  language: string
  dirty: boolean
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

  const loadTree = useCallback(async () => {
    try {
      const t = await todos.workspace.tree(todoId)
      setTree(t)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load file tree')
    }
  }, [todoId])

  const loadGitStatus = useCallback(async () => {
    try {
      const s = await todos.workspace.gitStatus(todoId)
      setGitStatus(s)
    } catch {
      // Ignore git status errors
    }
  }, [todoId])

  useEffect(() => {
    Promise.all([loadTree(), loadGitStatus()]).finally(() => setLoading(false))
  }, [loadTree, loadGitStatus])

  const handleFileSelect = useCallback(async (path: string) => {
    // Check if tab already open
    const existing = tabs.find(t => t.path === path)
    if (existing) {
      setActiveTab(path)
      return
    }

    try {
      const file = await todos.workspace.file(todoId, path)
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
      setTabs(prev => [...prev, newTab])
      setActiveTab(path)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to open file')
    }
  }, [tabs, todoId])

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
    if (!tab || !tab.dirty) return

    try {
      await todos.workspace.saveFile(todoId, tab.path, tab.content)
      setTabs(prev => prev.map(t =>
        t.path === tab.path
          ? { ...t, savedContent: t.content, dirty: false }
          : t
      ))
      loadGitStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save file')
    }
  }, [activeTab, tabs, todoId, loadGitStatus])

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

  const modifiedPaths = new Set(gitStatus.files.map(f => f.path))
  const currentTab = tabs.find(t => t.path === activeTab)
  const fileName = (path: string) => path.split('/').pop() || path

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
        <FileTree
          tree={tree}
          activeFile={activeTab}
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
              onClick={() => setActiveTab(tab.path)}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-xs border-r border-gray-800 shrink-0 transition-colors ${
                activeTab === tab.path
                  ? 'bg-gray-900 text-white border-b-2 border-b-indigo-500 -mb-px'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
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
          {/* Editor */}
          <div className="flex-1 min-h-0">
            {currentTab ? (
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
            collapsed={gitPanelCollapsed}
            onToggle={() => setGitPanelCollapsed(c => !c)}
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
