import { useEffect, useRef, useState, useCallback } from 'react'

export interface WSChatMessage {
  id?: string
  role: string
  content: string
  metadata_json?: Record<string, unknown>
  sender_name?: string
  created_at?: string
}

/**
 * WebSocket hook for real-time activity and token streaming during project chat.
 * Connects to /ws/chat/sessions/{sessionId} and surfaces:
 * - Tool call activity (what the agent is doing)
 * - Streaming LLM tokens in a collapsible panel (not inline as main content)
 * - Completed streaming text persisted for review after response
 * - New chat messages posted by the coordinator (e.g., task_plan_ready after re-plan)
 */
export function useChatSessionWebSocket(sessionId: string | null) {
  const ws = useRef<WebSocket | null>(null)
  const [activity, setActivity] = useState<string | null>(null)
  const [streamingText, setStreamingText] = useState('')
  const [completedStreaming, setCompletedStreaming] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [reconnectCount, setReconnectCount] = useState(0)
  const [incomingMessage, setIncomingMessage] = useState<WSChatMessage | null>(null)
  // Ref to track live token accumulation without relying on stale state in WS callback
  const streamingRef = useRef('')

  const clearActivity = useCallback(() => {
    setActivity(null)
    setStreamingText('')
    setIsStreaming(false)
    streamingRef.current = ''
    // completedStreaming intentionally NOT cleared — persists until next send
  }, [])

  /** Clear all streaming state — call before starting a new message send */
  const resetStreaming = useCallback(() => {
    setCompletedStreaming('')
    setStreamingText('')
    setIsStreaming(false)
    streamingRef.current = ''
  }, [])

  /** Clear the incoming message after it has been consumed */
  const clearIncomingMessage = useCallback(() => {
    setIncomingMessage(null)
  }, [])

  useEffect(() => {
    if (!sessionId) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${window.location.host}/ws/chat/sessions/${sessionId}`
    ws.current = new WebSocket(url)

    ws.current.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'activity' && data.activity) {
          setActivity(data.activity)
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
            setCompletedStreaming((prev) => (prev ? prev + '\n' + text : text))
          }
          streamingRef.current = ''
          setStreamingText('')
          setIsStreaming(false)
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
      setTimeout(() => {
        if (sessionId) {
          setReconnectCount((c) => c + 1)
        }
      }, 3000)
    }

    return () => {
      ws.current?.close()
      ws.current = null
    }
  }, [sessionId, reconnectCount])

  return {
    activity, streamingText, completedStreaming, isStreaming,
    clearActivity, resetStreaming,
    incomingMessage, clearIncomingMessage,
  }
}
