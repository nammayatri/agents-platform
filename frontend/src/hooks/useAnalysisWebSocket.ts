import { useEffect, useRef, useState, useCallback } from 'react'

interface AnalysisEvent {
  step: string
  detail: string
}

const STEP_ORDER = ['cloning', 'scanning', 'sampling', 'dependencies', 'analyzing', 'complete']

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
  const [completedSteps, setCompletedSteps] = useState<string[]>([])

  const onCompleteRef = useRef(onComplete)
  const onFailedRef = useRef(onFailed)
  onCompleteRef.current = onComplete
  onFailedRef.current = onFailed

  const reset = useCallback(() => {
    setCurrentStep(null)
    setDetail(null)
    setCompletedSteps([])
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
          setCompletedSteps(STEP_ORDER.filter(s => s !== 'complete'))
          setCurrentStep(null)
          setDetail(data.detail)
          onCompleteRef.current?.()
          return
        }

        if (data.step === 'failed') {
          setCurrentStep(null)
          setDetail(data.detail)
          onFailedRef.current?.(data.detail)
          return
        }

        // Mark all prior steps as completed
        const stepIdx = STEP_ORDER.indexOf(data.step)
        if (stepIdx >= 0) {
          setCompletedSteps(STEP_ORDER.slice(0, stepIdx))
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

  return { currentStep, detail, completedSteps, reset }
}
