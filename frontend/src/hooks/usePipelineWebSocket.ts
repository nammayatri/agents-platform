import { useEffect, useRef, useState } from 'react'
import type { PipelineRunStatus } from '../types'

export interface PipelineWSEvent {
  type: 'pipeline_status' | 'pipeline_progress' | 'ping'
  run_id?: string
  status?: PipelineRunStatus
  phase?: string
  message?: string
}

export function usePipelineWebSocket(projectId: string | null) {
  const ws = useRef<WebSocket | null>(null)
  const [lastEvent, setLastEvent] = useState<PipelineWSEvent | null>(null)
  const [reconnectCount, setReconnectCount] = useState(0)

  useEffect(() => {
    if (!projectId) return

    const boundProjectId = projectId
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${window.location.host}/ws/projects/${projectId}/pipeline`
    ws.current = new WebSocket(url)

    ws.current.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as PipelineWSEvent
        if (data.type !== 'ping') {
          setLastEvent(data)
        }
      } catch {
        // ignore parse errors
      }
    }

    ws.current.onclose = () => {
      if (boundProjectId === projectId) {
        setTimeout(() => setReconnectCount((c) => c + 1), 3000)
      }
    }

    return () => {
      ws.current?.close()
      ws.current = null
    }
  }, [projectId, reconnectCount])

  return { lastEvent }
}
