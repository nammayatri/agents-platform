import { useEffect, useRef, useState, useCallback } from 'react'

interface AnalysisEvent {
  step: string
  detail: string
}

/**
 * WebSocket hook for real-time project analysis progress.
 *
 * Connects to /ws/projects/{projectId}/analysis which implements
 * the **snapshot + stream** pattern:
 * - On connect, the server sends the current step from DB (survives refresh)
 * - Then streams incremental updates via Redis pub/sub
 *
 * This hook is intentionally step-order-agnostic — it just tracks
 * the current step and detail string. The rendering component owns
 * the STEP_ORDER and computes completed/pending from currentStep.
 */
export function useAnalysisWebSocket(
  projectId: string | null,
  active: boolean,
  onComplete?: () => void,
  onFailed?: (detail: string) => void,
) {
  const ws = useRef<WebSocket | null>(null)
  const [reconnectCount, setReconnectCount] = useState(0)
  const [currentStep, setCurrentStep] = useState<string | null>(null)
  const [detail, setDetail] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  const onCompleteRef = useRef(onComplete)
  const onFailedRef = useRef(onFailed)
  onCompleteRef.current = onComplete
  onFailedRef.current = onFailed

  const reset = useCallback(() => {
    setCurrentStep(null)
    setDetail(null)
    setDone(false)
  }, [])

  useEffect(() => {
    if (!projectId || !active) {
      ws.current?.close()
      ws.current = null
      return
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${window.location.host}/ws/projects/${projectId}/analysis`
    ws.current = new WebSocket(url)

    ws.current.onmessage = (event) => {
      try {
        const data: AnalysisEvent = JSON.parse(event.data)
        if (!data.step) return // ignore pings

        if (data.step === 'complete') {
          setCurrentStep(null)
          setDetail(data.detail)
          setDone(true)
          onCompleteRef.current?.()
          return
        }

        if (data.step === 'failed') {
          setCurrentStep(null)
          setDetail(data.detail)
          onFailedRef.current?.(data.detail)
          return
        }

        setCurrentStep(data.step)
        setDetail(data.detail)
      } catch {
        // ignore parse errors
      }
    }

    ws.current.onclose = () => {
      if (active) {
        setTimeout(() => setReconnectCount(c => c + 1), 3000)
      }
    }

    return () => {
      ws.current?.close()
      ws.current = null
    }
  }, [projectId, active, reconnectCount])

  return { currentStep, detail, done, reset }
}
