import { useEffect, useRef, useState, useCallback } from 'react'

/**
 * WebSocket hook for real-time activity updates during project chat.
 * Connects to /ws/chat/sessions/{sessionId} and surfaces tool call
 * activity so the UI can show what the agent is doing instead of
 * a static "Thinking..." indicator.
 */
export function useChatSessionWebSocket(sessionId: string | null) {
  const ws = useRef<WebSocket | null>(null)
  const [activity, setActivity] = useState<string | null>(null)
  const [reconnectCount, setReconnectCount] = useState(0)

  const clearActivity = useCallback(() => setActivity(null), [])

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

  return { activity, clearActivity }
}
