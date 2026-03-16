import { useEffect, useRef, useState, useCallback } from 'react'
import { useTodoStore } from '../stores/todoStore'
import type { WSEvent, ChatMessage } from '../types'

export function useTaskWebSocket(todoId: string | null) {
  const ws = useRef<WebSocket | null>(null)
  const [reconnectCount, setReconnectCount] = useState(0)
  const { updateTodoState, appendChatMessage, updateSubTaskProgress, batchAppendActivity, setLlmResponse, fetchTodo, fetchAgentRuns } = useTodoStore()
  const fetchDebounce = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Batch activity events: collect them and flush via rAF to avoid per-event re-renders
  const activityBuffer = useRef<Record<string, string[]>>({})
  const activityFlushScheduled = useRef(false)

  const flushActivity = useCallback(() => {
    activityFlushScheduled.current = false
    const buf = activityBuffer.current
    activityBuffer.current = {}
    const entries = Object.entries(buf)
    if (entries.length > 0) {
      batchAppendActivity(entries)
    }
  }, [batchAppendActivity])

  const queueActivity = useCallback((subTaskId: string, activity: string) => {
    if (!activityBuffer.current[subTaskId]) {
      activityBuffer.current[subTaskId] = []
    }
    activityBuffer.current[subTaskId].push(activity)
    if (!activityFlushScheduled.current) {
      activityFlushScheduled.current = true
      requestAnimationFrame(flushActivity)
    }
  }, [flushActivity])

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
              updateTodoState(todoId, data.state, data.error_message, data.sub_state)
              // Refetch full todo to get updated sub-tasks
              fetchTodo(todoId)
              // On terminal states, refetch agent runs for detailed error info
              if (data.state === 'failed' || data.state === 'completed') {
                fetchAgentRuns(todoId)
              }
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
              queueActivity(data.sub_task_id, data.activity)
            }
            break

          case 'llm_response':
            if (data.sub_task_id && data.content) {
              setLlmResponse(data.sub_task_id, data.content, data.iteration ?? 0)
            }
            break

          case 'task_cancelled':
            // Task was cancelled — refetch to get updated state and subtask statuses
            fetchTodo(todoId)
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
  }, [todoId, reconnectCount, updateTodoState, appendChatMessage, updateSubTaskProgress, setLlmResponse, fetchTodo, fetchAgentRuns, debouncedFetchTodo, queueActivity])
}
