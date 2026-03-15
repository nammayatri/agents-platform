import { useEffect, useRef, useState, useCallback } from 'react'
import { useTodoStore } from '../stores/todoStore'
import type { WSEvent, ChatMessage } from '../types'

export function useTaskWebSocket(todoId: string | null) {
  const ws = useRef<WebSocket | null>(null)
  const [reconnectCount, setReconnectCount] = useState(0)
  const { updateTodoState, appendChatMessage, updateSubTaskProgress, appendActivity, fetchTodo } = useTodoStore()
  const fetchDebounce = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Debounced fetchTodo to avoid hammering API on rapid subtask transitions
  const debouncedFetchTodo = useCallback((id: string) => {
    if (fetchDebounce.current) clearTimeout(fetchDebounce.current)
    fetchDebounce.current = setTimeout(() => {
      fetchTodo(id)
      fetchDebounce.current = null
    }, 500)
  }, [fetchTodo])

  useEffect(() => {
    if (!todoId) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${window.location.host}/ws/todos/${todoId}`
    ws.current = new WebSocket(url)

    ws.current.onmessage = (event) => {
      try {
        const data: WSEvent = JSON.parse(event.data)

        switch (data.type) {
          case 'state_change':
            if (data.state) {
              updateTodoState(todoId, data.state)
              // Refetch full todo to get updated sub-tasks
              fetchTodo(todoId)
            }
            break

          case 'subtask_update':
            // Subtask status changed — debounced refetch to get updated sub-tasks
            debouncedFetchTodo(todoId)
            break

          case 'chat_message':
            if (data.message && typeof data.message === 'object') {
              appendChatMessage(todoId, {
                id: data.message.id || crypto.randomUUID(),
                todo_id: todoId,
                role: data.message.role as ChatMessage['role'],
                content: data.message.content,
                created_at: new Date().toISOString(),
              })
            }
            break

          case 'progress':
            if (data.sub_task_id && data.progress_pct !== undefined) {
              const msg = typeof data.message === 'string'
                ? data.message
                : (data.message?.content || '')
              updateSubTaskProgress(todoId, data.sub_task_id, data.progress_pct, msg)
            }
            break

          case 'activity':
            if (data.sub_task_id && data.activity) {
              appendActivity(data.sub_task_id, data.activity)
            }
            break

          case 'ping':
            break
        }
      } catch {
        // Ignore parse errors
      }
    }

    ws.current.onclose = () => {
      // Reconnect after 3 seconds
      setTimeout(() => {
        if (todoId) {
          setReconnectCount((c) => c + 1)
        }
      }, 3000)
    }

    return () => {
      ws.current?.close()
      ws.current = null
      if (fetchDebounce.current) clearTimeout(fetchDebounce.current)
    }
  }, [todoId, reconnectCount, updateTodoState, appendChatMessage, updateSubTaskProgress, appendActivity, fetchTodo, debouncedFetchTodo])
}
