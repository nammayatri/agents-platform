import { useCallback } from 'react'
import Editor from '@monaco-editor/react'

interface MarkdownEditorProps {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  minHeight?: number
  maxHeight?: number
  readOnly?: boolean
  language?: string
}

export default function MarkdownEditor({
  value, onChange, placeholder,
  minHeight = 200, maxHeight = 600,
  readOnly = false, language = 'markdown',
}: MarkdownEditorProps) {
  const handleMount = useCallback((editor: any) => {
    const updateHeight = () => {
      const contentHeight = Math.min(
        Math.max(editor.getContentHeight(), minHeight),
        maxHeight,
      )
      const container = editor.getDomNode()
      if (container) {
        container.style.height = `${contentHeight}px`
      }
      editor.layout()
    }

    editor.onDidContentSizeChange(updateHeight)
    updateHeight()
  }, [minHeight, maxHeight])

  return (
    <div className="rounded-lg overflow-hidden border border-gray-800 focus-within:border-indigo-500 transition-colors">
      <Editor
        value={value || ''}
        defaultValue={placeholder}
        language={language}
        theme="vs-dark"
        onChange={(v) => onChange(v || '')}
        onMount={handleMount}
        options={{
          minimap: { enabled: false },
          fontSize: 13,
          lineNumbers: 'off',
          folding: false,
          wordWrap: 'on',
          wrappingStrategy: 'advanced',
          scrollBeyondLastLine: false,
          renderLineHighlight: 'none',
          overviewRulerBorder: false,
          overviewRulerLanes: 0,
          hideCursorInOverviewRuler: true,
          scrollbar: {
            vertical: 'auto',
            horizontal: 'hidden',
            verticalScrollbarSize: 8,
          },
          padding: { top: 12, bottom: 12 },
          readOnly,
          domReadOnly: readOnly,
          bracketPairColorization: { enabled: false },
          guides: { indentation: false },
          contextmenu: false,
          quickSuggestions: false,
          suggestOnTriggerCharacters: false,
          parameterHints: { enabled: false },
          tabCompletion: 'off',
        }}
      />
    </div>
  )
}
