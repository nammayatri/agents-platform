import { useState, useCallback, useEffect, useRef } from 'react'
import { projects as projectsApi } from '../../services/api'
import { useAnalysisWebSocket } from '../../hooks/useAnalysisWebSocket'

interface ProjectUnderstanding {
  summary?: string
  purpose?: string
  architecture?: string
  tech_stack?: string[]
  key_patterns?: string[]
  dependency_map?: { name: string; role: string }[]
  api_surface?: string
  testing_approach?: string
  important_context?: string[]
}

interface ProjectUnderstandingTabProps {
  projectId: string
  repoUrl: string
  analysisStatus: string | null
  projectUnderstanding: ProjectUnderstanding | null
  setAnalysisStatus: (status: string | null) => void
  setProjectUnderstanding: (u: ProjectUnderstanding | null) => void
  setError: (err: string) => void
}

const STEP_LABELS: Record<string, string> = {
  cloning: 'Clone repository',
  scanning: 'Scan codebase',
  sampling: 'Sample files',
  dependencies: 'Read dependencies',
  analyzing: 'LLM analysis',
}

const STEP_ORDER = ['cloning', 'scanning', 'sampling', 'dependencies', 'analyzing']

export default function ProjectUnderstandingTab({
  projectId, repoUrl, analysisStatus, projectUnderstanding,
  setAnalysisStatus, setProjectUnderstanding, setError,
}: ProjectUnderstandingTabProps) {
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const isAnalyzing = analysisStatus === 'analyzing'

  const handleComplete = useCallback(async () => {
    // Fetch the final project data to get the understanding
    try {
      const p = await projectsApi.get(projectId)
      const settings = typeof p.settings_json === 'string' ? JSON.parse(p.settings_json) : p.settings_json
      setAnalysisStatus(settings?.analysis_status || 'complete')
      setProjectUnderstanding(settings?.project_understanding || null)
    } catch {
      setAnalysisStatus('complete')
    }
  }, [projectId, setAnalysisStatus, setProjectUnderstanding])

  const handleFailed = useCallback((detail: string) => {
    setAnalysisStatus('failed')
    setError(detail || 'Analysis failed')
  }, [setAnalysisStatus, setError])

  const { currentStep, detail, completedSteps, reset } = useAnalysisWebSocket(
    projectId, isAnalyzing, handleComplete, handleFailed,
  )

  // Polling fallback: check DB status every 5s when analyzing,
  // in case WebSocket misses the completion/failure event
  const setAnalysisStatusRef = useRef(setAnalysisStatus)
  const setProjectUnderstandingRef = useRef(setProjectUnderstanding)
  setAnalysisStatusRef.current = setAnalysisStatus
  setProjectUnderstandingRef.current = setProjectUnderstanding

  useEffect(() => {
    if (!isAnalyzing || !projectId) return
    const interval = setInterval(async () => {
      try {
        const p = await projectsApi.get(projectId)
        const settings = typeof p.settings_json === 'string' ? JSON.parse(p.settings_json) : p.settings_json
        const status = settings?.analysis_status
        if (status && status !== 'analyzing') {
          setAnalysisStatusRef.current(status)
          if (status === 'complete') {
            setProjectUnderstandingRef.current(settings?.project_understanding || null)
          }
        }
      } catch { /* ignore polling errors */ }
    }, 5000)
    return () => clearInterval(interval)
  }, [isAnalyzing, projectId])

  const handleCancel = async () => {
    if (!projectId) return
    setCancelling(true)
    try {
      await projectsApi.cancelAnalysis(projectId)
      setAnalysisStatus(null)
      reset()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to cancel')
    } finally {
      setCancelling(false)
    }
  }

  return (
    <>
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-600">AI-generated understanding of the project from its repository.</p>
        {repoUrl && !isAnalyzing && (
          <button
            onClick={async () => {
              if (!projectId) return
              setStarting(true)
              try {
                await projectsApi.analyze(projectId)
                setAnalysisStatus('analyzing')
                reset()
              } catch (e) { setError(e instanceof Error ? e.message : 'Analysis failed') }
              finally { setStarting(false) }
            }}
            disabled={starting}
            className="text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-40 transition-colors shrink-0"
          >
            {starting ? 'Starting...' : analysisStatus === 'complete' ? 'Re-analyze' : 'Analyze'}
          </button>
        )}
      </div>

      {!repoUrl && (
        <div className="py-4 text-center text-xs text-gray-600 border border-dashed border-gray-800 rounded-lg">
          Add a repository URL in the Repository tab to enable project analysis.
        </div>
      )}

      {repoUrl && !analysisStatus && (
        <div className="py-4 text-center text-xs text-gray-600 border border-dashed border-gray-800 rounded-lg">
          Not yet analyzed. Click &quot;Analyze&quot; to start.
        </div>
      )}

      {isAnalyzing && (
        <div className="px-4 py-3 bg-indigo-500/5 border border-indigo-500/10 rounded-lg space-y-2">
          {STEP_ORDER.map((step) => {
            const isCompleted = completedSteps.includes(step)
            const isCurrent = currentStep === step
            const isPending = !isCompleted && !isCurrent

            return (
              <div key={step} className="flex items-center gap-2.5">
                {isCompleted && (
                  <svg className="w-3.5 h-3.5 text-emerald-400 shrink-0" viewBox="0 0 16 16" fill="currentColor">
                    <path fillRule="evenodd" d="M8 16A8 8 0 108 0a8 8 0 000 16zm3.78-9.72a.75.75 0 00-1.06-1.06L6.75 9.19 5.28 7.72a.75.75 0 00-1.06 1.06l2 2a.75.75 0 001.06 0l4.5-4.5z" />
                  </svg>
                )}
                {isCurrent && (
                  <svg className="w-3.5 h-3.5 animate-spin text-indigo-400 shrink-0" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                    <path className="opacity-80" d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
                  </svg>
                )}
                {isPending && (
                  <div className="w-3.5 h-3.5 flex items-center justify-center shrink-0">
                    <div className="w-1.5 h-1.5 rounded-full bg-gray-700" />
                  </div>
                )}
                <span className={`text-xs ${isCurrent ? 'text-indigo-300' : isCompleted ? 'text-gray-500' : 'text-gray-700'}`}>
                  {STEP_LABELS[step]}
                </span>
                {isCurrent && detail && (
                  <span className="text-[11px] text-gray-600 ml-auto">{detail}</span>
                )}
              </div>
            )
          })}
          <div className="pt-1 flex justify-end">
            <button
              onClick={handleCancel}
              disabled={cancelling}
              className="text-[11px] text-gray-600 hover:text-gray-400 disabled:opacity-40 transition-colors"
            >
              {cancelling ? 'Cancelling...' : 'Cancel'}
            </button>
          </div>
        </div>
      )}

      {analysisStatus === 'failed' && (
        <div className="px-4 py-3 bg-red-500/5 border border-red-500/10 rounded-lg text-sm text-red-300/60">
          Analysis failed. Check that the repository URL is accessible.
        </div>
      )}

      {analysisStatus === 'no_docs' && (
        <div className="px-4 py-3 bg-gray-900 border border-gray-800 rounded-lg text-sm text-gray-500">
          No documentation files found in the repository.
        </div>
      )}

      {analysisStatus === 'complete' && projectUnderstanding && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden divide-y divide-gray-800">
          {projectUnderstanding.summary && (
            <div className="px-4 py-3"><p className="text-sm text-gray-300 leading-relaxed">{projectUnderstanding.summary}</p></div>
          )}
          {projectUnderstanding.purpose && (
            <div className="px-4 py-3">
              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Purpose</span>
              <p className="text-sm text-gray-400 mt-1 leading-relaxed">{projectUnderstanding.purpose}</p>
            </div>
          )}
          {projectUnderstanding.architecture && (
            <div className="px-4 py-3">
              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Architecture</span>
              <p className="text-sm text-gray-400 mt-1 leading-relaxed whitespace-pre-line">{projectUnderstanding.architecture}</p>
            </div>
          )}
          {projectUnderstanding.tech_stack && projectUnderstanding.tech_stack.length > 0 && (
            <div className="px-4 py-3">
              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Tech Stack</span>
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                {projectUnderstanding.tech_stack.map((t, i) => (
                  <span key={i} className="px-2 py-0.5 bg-gray-800 rounded text-[11px] text-gray-400">{t}</span>
                ))}
              </div>
            </div>
          )}
          {projectUnderstanding.key_patterns && projectUnderstanding.key_patterns.length > 0 && (
            <div className="px-4 py-3">
              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Key Patterns</span>
              <ul className="mt-1.5 space-y-1">
                {projectUnderstanding.key_patterns.map((p, i) => (
                  <li key={i} className="text-sm text-gray-400 flex items-start gap-2"><span className="w-1 h-1 rounded-full bg-gray-600 mt-2 shrink-0" />{p}</li>
                ))}
              </ul>
            </div>
          )}
          {projectUnderstanding.dependency_map && projectUnderstanding.dependency_map.length > 0 && (
            <div className="px-4 py-3">
              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Dependency Roles</span>
              <div className="mt-1.5 space-y-1">
                {projectUnderstanding.dependency_map.map((d, i) => (
                  <div key={i} className="flex items-baseline gap-2 text-sm">
                    <span className="text-gray-300 font-mono text-xs shrink-0">{d.name}</span>
                    <span className="text-gray-600">--</span>
                    <span className="text-gray-500">{d.role}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </>
  )
}
