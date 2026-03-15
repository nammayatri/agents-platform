import { useEffect, useRef, useState, useCallback } from 'react'

export interface WSChatMessage {
  id?: string
  role: string
  content: string
  metadata_json?: Record<string, unknown>
  sender_name?: string
  created_at?: string
}

/** sessionStorage key for persisting streaming state per session */
function storageKey(sessionId: string) {
  return `chat_ws:${sessionId}`
}

interface PersistedStreamState {
  activityLog: string[]
  completedStreaming: string
}

function persistState(sessionId: string, log: string[], completed: string) {
  try {
    sessionStorage.setItem(storageKey(sessionId), JSON.stringify({
      activityLog: log,
      completedStreaming: completed,
    }))
  } catch { /* quota errors etc */ }
}

function restoreState(sessionId: string): PersistedStreamState | null {
  try {
    const raw = sessionStorage.getItem(storageKey(sessionId))
    if (!raw) return null
    return JSON.parse(raw) as PersistedStreamState
  } catch {
    return null
  }
}

function clearState(sessionId: string) {
  try { sessionStorage.removeItem(storageKey(sessionId)) } catch {}
}

/**
 * WebSocket hook for real-time activity and token streaming during project chat.
 * Connects to /ws/chat/sessions/{sessionId} and surfaces:
 * - Tool call activity (what the agent is doing)
 * - Streaming LLM tokens in a collapsible panel (not inline as main content)
 * - Completed streaming text persisted for review after response
 * - New chat messages posted by the coordinator (e.g., task_plan_ready after re-plan)
 *
 * Activity log and completed streaming text are persisted to sessionStorage
 * so they survive page refreshes within the same browser tab.
 */
export function useChatSessionWebSocket(sessionId: string | null) {
  const ws = useRef<WebSocket | null>(null)
  const [activity, setActivity] = useState<string | null>(null)
  const [activityLog, setActivityLog] = useState<string[]>([])
  const [streamingText, setStreamingText] = useState('')
  const [completedStreaming, setCompletedStreaming] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [reconnectCount, setReconnectCount] = useState(0)
  const [incomingMessage, setIncomingMessage] = useState<WSChatMessage | null>(null)
  // Refs to track live state without relying on stale closures in WS callback
  const streamingRef = useRef('')
  const activityLogRef = useRef<string[]>([])
  const completedStreamingRef = useRef('')
  // Track the active session ID so onmessage handlers from stale connections
  // can detect they belong to a previous session and discard events.
  const activeSessionRef = useRef(sessionId)

  const clearActivity = useCallback(() => {
    setActivity(null)
    setStreamingText('')
    setIsStreaming(false)
    streamingRef.current = ''
    // completedStreaming + activityLog intentionally NOT cleared — persists until next send
  }, [])

  /** Clear all streaming state — call before starting a new message send */
  const resetStreaming = useCallback(() => {
    setCompletedStreaming('')
    setStreamingText('')
    setIsStreaming(false)
    setActivity(null)
    setActivityLog([])
    streamingRef.current = ''
    activityLogRef.current = []
    completedStreamingRef.current = ''
    if (activeSessionRef.current) {
      clearState(activeSessionRef.current)
    }
  }, [])

  /** Clear the incoming message after it has been consumed */
  const clearIncomingMessage = useCallback(() => {
    setIncomingMessage(null)
  }, [])

  // Reset all state when the session changes (prevents cross-session leakage)
  // Then restore any persisted data for the new session.
  useEffect(() => {
    activeSessionRef.current = sessionId
    setActivity(null)
    setStreamingText('')
    setIsStreaming(false)
    setIncomingMessage(null)
    streamingRef.current = ''

    // Restore persisted streaming state for this session
    if (sessionId) {
      const stored = restoreState(sessionId)
      if (stored) {
        setActivityLog(stored.activityLog)
        activityLogRef.current = stored.activityLog
        setCompletedStreaming(stored.completedStreaming)
        completedStreamingRef.current = stored.completedStreaming
      } else {
        setActivityLog([])
        activityLogRef.current = []
        setCompletedStreaming('')
        completedStreamingRef.current = ''
      }
    } else {
      setActivityLog([])
      activityLogRef.current = []
      setCompletedStreaming('')
      completedStreamingRef.current = ''
    }
  }, [sessionId])

  useEffect(() => {
    if (!sessionId) return

    // Capture the session ID for this specific effect invocation
    const boundSessionId = sessionId

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${window.location.host}/ws/chat/sessions/${sessionId}`
    ws.current = new WebSocket(url)

    ws.current.onmessage = (event) => {
      // Guard: discard events from a stale connection after session switch
      if (activeSessionRef.current !== boundSessionId) return

      try {
        const data = JSON.parse(event.data)
        if (data.type === 'activity' && data.activity) {
          setActivity(data.activity)
          // Append to activity log (dedup consecutive identical entries)
          const log = activityLogRef.current
          if (log.length === 0 || log[log.length - 1] !== data.activity) {
            const newLog = [...log, data.activity]
            activityLogRef.current = newLog
            setActivityLog(newLog)
            persistState(boundSessionId, newLog, completedStreamingRef.current)
          }
        } else if (data.type === 'token' && data.token) {
          // Accumulate live streaming text
          streamingRef.current += data.token
          setStreamingText(streamingRef.current)
          setIsStreaming(true)
          setActivity(null)
        } else if (data.type === 'token_done') {
          // Round complete: move live text to completed, clear live buffer
          if (streamingRef.current) {
            const text = streamingRef.current
            const newCompleted = completedStreamingRef.current
              ? completedStreamingRef.current + '\n' + text
              : text
            completedStreamingRef.current = newCompleted
            setCompletedStreaming(newCompleted)
          }
          streamingRef.current = ''
          setStreamingText('')
          setIsStreaming(false)
          persistState(boundSessionId, activityLogRef.current, completedStreamingRef.current)
        } else if (data.type === 'chat_message' && data.message) {
          // New message from coordinator (e.g., task_plan_ready after re-plan)
          setIncomingMessage(data.message as WSChatMessage)
        }
        // ignore pings
      } catch {
        // Ignore parse errors
      }
    }

    ws.current.onclose = () => {
      // Only reconnect if this is still the active session
      if (activeSessionRef.current === boundSessionId) {
        setTimeout(() => {
          setReconnectCount((c) => c + 1)
        }, 3000)
      }
    }

    return () => {
      ws.current?.close()
      ws.current = null
    }
  }, [sessionId, reconnectCount])

  return {
    activity, activityLog, streamingText, completedStreaming, isStreaming,
    clearActivity, resetStreaming,
    incomingMessage, clearIncomingMessage,
  }
}
