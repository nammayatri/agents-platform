import { useState, useCallback } from 'react'
import { ChevronRight, ChevronDown, File, Folder, FolderOpen } from 'lucide-react'
import type { FileTreeNode } from '../../types'

interface Props {
  tree: FileTreeNode[]
  activeFile: string | null
  modifiedPaths: Set<string>
  onFileSelect: (path: string) => void
}

const EXT_COLORS: Record<string, string> = {
  '.ts': 'text-blue-400', '.tsx': 'text-blue-400',
  '.js': 'text-yellow-400', '.jsx': 'text-yellow-400', '.mjs': 'text-yellow-400',
  '.py': 'text-green-400', '.pyw': 'text-green-400',
  '.json': 'text-amber-400',
  '.md': 'text-gray-400',
  '.css': 'text-pink-400', '.scss': 'text-pink-400',
  '.html': 'text-orange-400',
  '.rs': 'text-orange-500',
  '.go': 'text-cyan-400',
  '.java': 'text-red-400',
  '.rb': 'text-red-500',
  '.sql': 'text-purple-400',
  '.yaml': 'text-green-300', '.yml': 'text-green-300',
  '.sh': 'text-green-500',
}

function getFileColor(name: string): string {
  const ext = name.includes('.') ? '.' + name.split('.').pop()! : ''
  return EXT_COLORS[ext.toLowerCase()] || 'text-gray-500'
}

function TreeNode({
  node, depth, activeFile, modifiedPaths, onFileSelect, defaultExpanded,
}: {
  node: FileTreeNode
  depth: number
  activeFile: string | null
  modifiedPaths: Set<string>
  onFileSelect: (path: string) => void
  defaultExpanded: boolean
}) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const isDir = node.type === 'dir'
  const isActive = activeFile === node.path
  const isModified = modifiedPaths.has(node.path)

  const handleClick = useCallback(() => {
    if (isDir) {
      setExpanded(e => !e)
    } else {
      onFileSelect(node.path)
    }
  }, [isDir, node.path, onFileSelect])

  return (
    <div>
      <button
        onClick={handleClick}
        className={`w-full flex items-center gap-1 px-2 py-[3px] text-left text-xs hover:bg-gray-800/60 transition-colors ${
          isActive ? 'bg-gray-800/80 text-white' : 'text-gray-400'
        }`}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
      >
        {isDir ? (
          <>
            {expanded ? (
              <ChevronDown className="w-3 h-3 text-gray-600 shrink-0" />
            ) : (
              <ChevronRight className="w-3 h-3 text-gray-600 shrink-0" />
            )}
            {expanded ? (
              <FolderOpen className="w-3.5 h-3.5 text-amber-500/70 shrink-0" />
            ) : (
              <Folder className="w-3.5 h-3.5 text-amber-500/70 shrink-0" />
            )}
          </>
        ) : (
          <>
            <span className="w-3 shrink-0" />
            <File className={`w-3.5 h-3.5 shrink-0 ${getFileColor(node.name)}`} />
          </>
        )}
        <span className="truncate">{node.name}</span>
        {isModified && (
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0 ml-auto" />
        )}
      </button>
      {isDir && expanded && node.children && (
        <div>
          {node.children.map(child => (
            <TreeNode
              key={child.path}
              node={child}
              depth={depth + 1}
              activeFile={activeFile}
              modifiedPaths={modifiedPaths}
              onFileSelect={onFileSelect}
              defaultExpanded={false}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export default function FileTree({ tree, activeFile, modifiedPaths, onFileSelect }: Props) {
  return (
    <div className="py-1 overflow-y-auto h-full text-[13px]">
      {tree.map(node => (
        <TreeNode
          key={node.path}
          node={node}
          depth={0}
          activeFile={activeFile}
          modifiedPaths={modifiedPaths}
          onFileSelect={onFileSelect}
          defaultExpanded={true}
        />
      ))}
      {tree.length === 0 && (
        <div className="px-4 py-8 text-xs text-gray-600 text-center">
          No files found
        </div>
      )}
    </div>
  )
}
