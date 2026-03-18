import { useEffect, useRef, useState, useCallback } from 'react'

/**
 * WebSocket hook for real-time activity and token streaming during project chat.
 * Connects to /ws/chat/sessions/{sessionId} and surfaces:
 * - Tool call activity (what the agent is doing)
 * - Streaming LLM tokens (what the agent is thinking/writing)
 */
export function useChatSessionWebSocket(sessionId: string | null) {
  const ws = useRef<WebSocket | null>(null)
  const [activity, setActivity] = useState<string | null>(null)
  const [streamingText, setStreamingText] = useState('')
  const [reconnectCount, setReconnectCount] = useState(0)

  const clearActivity = useCallback(() => {
    setActivity(null)
    setStreamingText('')
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
          // Clear streaming text when activity arrives (tool execution started)
          setStreamingText('')
        } else if (data.type === 'token' && data.token) {
          // Accumulate streaming text from LLM
          setStreamingText((prev) => prev + data.token)
          // Clear activity while tokens are streaming
          setActivity(null)
        } else if (data.type === 'token_done') {
          // Streaming round complete — clear for next round
          setStreamingText('')
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

  return { activity, streamingText, clearActivity }
}
