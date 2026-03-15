import { useState, useMemo } from 'react'

interface DiffViewerProps {
  diff: string
  stats?: string
  files?: Array<{ status: string; path: string }>
  maxHeight?: string
}

interface DiffLine {
  type: 'add' | 'delete' | 'context'
  content: string
  oldNum?: number
  newNum?: number
}

interface DiffHunk {
  oldStart: number
  newStart: number
  header: string
  lines: DiffLine[]
}

interface DiffFile {
  path: string
  status: string
  hunks: DiffHunk[]
}

function parseDiff(raw: string, fileList?: Array<{ status: string; path: string }>): DiffFile[] {
  if (!raw || !raw.trim()) return []

  const fileMap = new Map<string, string>()
  if (fileList) {
    for (const f of fileList) {
      fileMap.set(f.path, f.status)
    }
  }

  // Split on file boundaries
  const sections = raw.split(/^(?=diff --git a\/)/m).filter((s) => s.trim())
  const files: DiffFile[] = []

  for (const section of sections) {
    const lines = section.split('\n')
    if (!lines[0]?.startsWith('diff --git')) continue

    // Extract path from "diff --git a/path b/path"
    const gitMatch = lines[0].match(/^diff --git a\/.+ b\/(.+)$/)
    if (!gitMatch) continue
    const path = gitMatch[1]

    // Skip binary files
    if (section.includes('Binary files') || section.includes('GIT binary patch')) {
      files.push({ path, status: fileMap.get(path) || 'M', hunks: [] })
      continue
    }

    // Detect status
    let status = fileMap.get(path) || 'M'
    let headerPassed = false
    const hunks: DiffHunk[] = []

    for (let i = 1; i < lines.length; i++) {
      const line = lines[i]

      if (!headerPassed) {
        if (line.startsWith('--- /dev/null')) {
          status = fileMap.get(path) || 'A'
        } else if (line.startsWith('+++ /dev/null')) {
          status = fileMap.get(path) || 'D'
        }
        if (line.startsWith('@@')) {
          headerPassed = true
        } else {
          continue
        }
      }

      // Parse hunk header
      if (line.startsWith('@@')) {
        const hunkMatch = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/)
        if (hunkMatch) {
          hunks.push({
            oldStart: parseInt(hunkMatch[1], 10),
            newStart: parseInt(hunkMatch[2], 10),
            header: line,
            lines: [],
          })
        }
        continue
      }

      if (hunks.length === 0) continue
      const currentHunk = hunks[hunks.length - 1]

      // Skip "no newline" markers
      if (line.startsWith('\\')) continue

      const prefix = line[0]
      const content = line.slice(1)

      // Track line numbers
      const lastLine = currentHunk.lines[currentHunk.lines.length - 1]
      let oldNum: number | undefined
      let newNum: number | undefined

      if (currentHunk.lines.length === 0) {
        oldNum = currentHunk.oldStart
        newNum = currentHunk.newStart
      } else {
        oldNum = lastLine?.oldNum ?? currentHunk.oldStart - 1
        newNum = lastLine?.newNum ?? currentHunk.newStart - 1
        if (lastLine?.type === 'context') {
          oldNum = oldNum + 1
          newNum = newNum + 1
        } else if (lastLine?.type === 'add') {
          newNum = newNum + 1
        } else if (lastLine?.type === 'delete') {
          oldNum = oldNum + 1
        }
      }

      if (prefix === '+') {
        currentHunk.lines.push({ type: 'add', content, newNum, oldNum: undefined })
      } else if (prefix === '-') {
        currentHunk.lines.push({ type: 'delete', content, oldNum, newNum: undefined })
      } else if (prefix === ' ' || prefix === undefined) {
        currentHunk.lines.push({ type: 'context', content: content ?? '', oldNum, newNum })
      }
    }

    files.push({ path, status, hunks })
  }

  return files
}

const STATUS_COLORS: Record<string, string> = {
  M: 'bg-gray-800 text-gray-400',
  A: 'bg-emerald-500/10 text-emerald-400',
  D: 'bg-red-500/10 text-red-400',
  R: 'bg-amber-500/10 text-amber-400',
}

const LINE_COLORS: Record<string, string> = {
  add: 'bg-emerald-500/5 text-emerald-300/80',
  delete: 'bg-red-500/5 text-red-300/80',
  context: 'text-gray-500',
}

export default function DiffViewer({ diff, stats, files, maxHeight = 'max-h-[600px]' }: DiffViewerProps) {
  const [collapsedFiles, setCollapsedFiles] = useState<Set<number>>(new Set())

  const parsedFiles = useMemo(() => parseDiff(diff, files), [diff, files])

  const totalAdditions = useMemo(
    () => parsedFiles.reduce((sum, f) => sum + f.hunks.reduce((hs, h) => hs + h.lines.filter((l) => l.type === 'add').length, 0), 0),
    [parsedFiles],
  )
  const totalDeletions = useMemo(
    () => parsedFiles.reduce((sum, f) => sum + f.hunks.reduce((hs, h) => hs + h.lines.filter((l) => l.type === 'delete').length, 0), 0),
    [parsedFiles],
  )

  const toggleFile = (idx: number) => {
    setCollapsedFiles((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx)
      else next.add(idx)
      return next
    })
  }

  if (parsedFiles.length === 0) {
    return (
      <div className="mt-2 py-4 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
        No diff available
      </div>
    )
  }

  return (
    <div className={`mt-2 rounded-lg border border-gray-800 overflow-hidden ${maxHeight} overflow-y-auto`}>
      {/* Stats header */}
      <div className="px-3 py-2 bg-gray-900 border-b border-gray-800 flex items-center gap-3 text-[11px] sticky top-0 z-10">
        <span className="text-gray-500">
          {parsedFiles.length} file{parsedFiles.length !== 1 ? 's' : ''} changed
        </span>
        {totalAdditions > 0 && <span className="text-emerald-400">+{totalAdditions}</span>}
        {totalDeletions > 0 && <span className="text-red-400">-{totalDeletions}</span>}
        {stats && <span className="text-gray-600 ml-auto font-mono truncate max-w-[50%]">{stats}</span>}
      </div>

      {/* File sections */}
      {parsedFiles.map((file, fileIdx) => {
        const collapsed = collapsedFiles.has(fileIdx)
        return (
          <div key={fileIdx}>
            {/* File header */}
            <div
              className="px-3 py-1.5 bg-gray-900/80 border-b border-gray-800 flex items-center gap-2 cursor-pointer hover:bg-gray-800/50 sticky top-[33px] z-[5]"
              onClick={() => toggleFile(fileIdx)}
            >
              <span className="text-gray-600 text-[11px]">{collapsed ? '\u25B6' : '\u25BC'}</span>
              <span
                className={`text-[10px] font-medium px-1 py-0.5 rounded ${STATUS_COLORS[file.status] || STATUS_COLORS.M}`}
              >
                {file.status}
              </span>
              <span className="text-[11px] text-gray-300 font-mono truncate">{file.path}</span>
            </div>

            {/* Hunks */}
            {!collapsed &&
              file.hunks.map((hunk, hunkIdx) => (
                <div key={hunkIdx}>
                  {/* Hunk header */}
                  <div className="text-[11px] text-indigo-400/60 bg-indigo-500/5 px-3 py-0.5 font-mono border-b border-gray-800/50">
                    {hunk.header}
                  </div>

                  {/* Lines */}
                  {hunk.lines.map((line, lineIdx) => (
                    <div
                      key={lineIdx}
                      className={`flex font-mono text-[11px] leading-[18px] ${LINE_COLORS[line.type]}`}
                    >
                      <span className="w-10 text-right text-gray-700 select-none shrink-0 pr-1 border-r border-gray-800/50">
                        {line.oldNum ?? ''}
                      </span>
                      <span className="w-10 text-right text-gray-700 select-none shrink-0 pr-1 border-r border-gray-800/50">
                        {line.newNum ?? ''}
                      </span>
                      <span className="w-5 text-center select-none shrink-0">
                        {line.type === 'add' ? '+' : line.type === 'delete' ? '-' : ' '}
                      </span>
                      <span className="flex-1 whitespace-pre overflow-x-auto">{line.content}</span>
                    </div>
                  ))}
                </div>
              ))}
          </div>
        )
      })}
    </div>
  )
}
