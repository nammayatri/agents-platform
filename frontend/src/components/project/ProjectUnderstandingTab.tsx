import { useState, useCallback, useEffect, useRef } from 'react'
import { projects as projectsApi } from '../../services/api'
import { useAnalysisWebSocket } from '../../hooks/useAnalysisWebSocket'
import type { DepUnderstanding, LinkingDocument, IndexMetadata } from '../../types'

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
  cloning: 'Fetch latest code',
  scanning: 'Scan codebase',
  sampling: 'Sample files',
  dependencies: 'Read dependencies',
  analyzing: 'LLM analysis',
  indexing: 'Build code indexes',
  dep_analysis: 'Analyze dependencies',
  linking: 'Build linking document',
  dep_indexing: 'Index dependency repos',
}

const STEP_ORDER = [
  'cloning', 'scanning', 'sampling', 'dependencies', 'analyzing',
  'indexing', 'dep_analysis', 'linking', 'dep_indexing',
]

export default function ProjectUnderstandingTab({
  projectId, repoUrl, analysisStatus, projectUnderstanding,
  setAnalysisStatus, setProjectUnderstanding, setError,
}: ProjectUnderstandingTabProps) {
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [depUnderstandings, setDepUnderstandings] = useState<Record<string, DepUnderstanding> | null>(null)
  const [linkingDocument, setLinkingDocument] = useState<(LinkingDocument & { summary?: string; raw?: boolean }) | null>(null)
  const [indexMetadata, setIndexMetadata] = useState<IndexMetadata | null>(null)
  const [expandedDeps, setExpandedDeps] = useState<Set<string>>(new Set())
  const isAnalyzing = analysisStatus === 'analyzing'

  const loadExtraData = useCallback(async () => {
    try {
      const p = await projectsApi.get(projectId)
      const settings = typeof p.settings_json === 'string' ? JSON.parse(p.settings_json) : p.settings_json
      setDepUnderstandings(settings?.dep_understandings || null)
      setLinkingDocument(settings?.linking_document || null)
      setIndexMetadata(settings?.index_metadata || null)
    } catch { /* ignore */ }
  }, [projectId])

  const handleComplete = useCallback(async () => {
    try {
      const p = await projectsApi.get(projectId)
      const settings = typeof p.settings_json === 'string' ? JSON.parse(p.settings_json) : p.settings_json
      setAnalysisStatus(settings?.analysis_status || 'complete')
      setProjectUnderstanding(settings?.project_understanding || null)
      setDepUnderstandings(settings?.dep_understandings || null)
      setLinkingDocument(settings?.linking_document || null)
      setIndexMetadata(settings?.index_metadata || null)
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

  // Load dep understandings when analysis is complete but dep data isn't loaded yet
  useEffect(() => {
    if (analysisStatus === 'complete' && projectId && !depUnderstandings) {
      loadExtraData()
    }
  }, [analysisStatus, projectId, depUnderstandings, loadExtraData])

  // Polling fallback: check DB status every 5s when analyzing
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
            setDepUnderstandings(settings?.dep_understandings || null)
            setLinkingDocument(settings?.linking_document || null)
            setIndexMetadata(settings?.index_metadata || null)
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

  const toggleDep = (name: string) => {
    setExpandedDeps(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const depEntries = depUnderstandings ? Object.entries(depUnderstandings) : []

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
        <div className="space-y-4">
          {/* Main repo understanding */}
          <div>
            <span className="text-[11px] text-gray-600 uppercase tracking-wider">Main Repository</span>
            <div className="mt-1.5 bg-gray-900 border border-gray-800 rounded-lg overflow-hidden divide-y divide-gray-800">
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
          </div>

          {/* Dependency understandings */}
          {depEntries.length > 0 && (
            <div>
              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Dependency Repos ({depEntries.length})</span>
              <div className="mt-1.5 space-y-1.5">
                {depEntries.map(([depName, dep]) => {
                  const isExpanded = expandedDeps.has(depName)
                  return (
                    <div key={depName} className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
                      <button
                        onClick={() => toggleDep(depName)}
                        className="w-full px-4 py-2.5 flex items-center justify-between hover:bg-gray-800/50 transition-colors"
                      >
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="text-sm text-gray-300 font-mono shrink-0">{depName}</span>
                          {dep.purpose && (
                            <span className="text-xs text-gray-600 truncate">{dep.purpose}</span>
                          )}
                        </div>
                        <svg
                          className={`w-3.5 h-3.5 text-gray-600 shrink-0 transition-transform ${isExpanded ? 'rotate-180' : ''}`}
                          viewBox="0 0 20 20" fill="currentColor"
                        >
                          <path fillRule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z" clipRule="evenodd" />
                        </svg>
                      </button>
                      {isExpanded && (
                        <div className="border-t border-gray-800 divide-y divide-gray-800">
                          {dep.summary && (
                            <div className="px-4 py-3">
                              <p className="text-sm text-gray-300 leading-relaxed">{dep.summary}</p>
                            </div>
                          )}
                          {dep.tech_stack && dep.tech_stack.length > 0 && (
                            <div className="px-4 py-3">
                              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Tech Stack</span>
                              <div className="flex flex-wrap gap-1.5 mt-1.5">
                                {dep.tech_stack.map((t, i) => (
                                  <span key={i} className="px-2 py-0.5 bg-gray-800 rounded text-[11px] text-gray-400">{t}</span>
                                ))}
                              </div>
                            </div>
                          )}
                          {dep.api_surface && (
                            <div className="px-4 py-3">
                              <span className="text-[11px] text-gray-600 uppercase tracking-wider">API Surface</span>
                              <p className="text-sm text-gray-400 mt-1 leading-relaxed whitespace-pre-line">{dep.api_surface}</p>
                            </div>
                          )}
                          {dep.key_patterns && dep.key_patterns.length > 0 && (
                            <div className="px-4 py-3">
                              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Key Patterns</span>
                              <ul className="mt-1.5 space-y-1">
                                {dep.key_patterns.map((p, i) => (
                                  <li key={i} className="text-sm text-gray-400 flex items-start gap-2"><span className="w-1 h-1 rounded-full bg-gray-600 mt-2 shrink-0" />{p}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                          {dep.exports && dep.exports.length > 0 && (
                            <div className="px-4 py-3">
                              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Exports</span>
                              <div className="flex flex-wrap gap-1.5 mt-1.5">
                                {dep.exports.map((e, i) => (
                                  <span key={i} className="px-2 py-0.5 bg-gray-800 rounded text-[11px] text-gray-400 font-mono">{e}</span>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Cross-repo linking document */}
          {linkingDocument && (
            <div>
              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Cross-Repo Architecture</span>
              <div className="mt-1.5 bg-gray-900 border border-gray-800 rounded-lg overflow-hidden divide-y divide-gray-800">
                {(linkingDocument.overview || linkingDocument.summary) && (
                  <div className="px-4 py-3">
                    <p className="text-sm text-gray-300 leading-relaxed whitespace-pre-line">
                      {linkingDocument.overview || linkingDocument.summary}
                    </p>
                  </div>
                )}
                {linkingDocument.integrations && linkingDocument.integrations.length > 0 && (
                  <div className="px-4 py-3">
                    <span className="text-[11px] text-gray-600 uppercase tracking-wider">Integrations</span>
                    <div className="mt-1.5 space-y-2">
                      {linkingDocument.integrations.map((intg, i) => (
                        <div key={i} className="px-3 py-2 bg-gray-800/50 rounded-lg">
                          <div className="flex items-center gap-2 text-xs">
                            <span className="text-gray-300 font-mono">{intg.source_repo}</span>
                            <span className="text-gray-600">-&gt;</span>
                            <span className="text-gray-300 font-mono">{intg.target_repo}</span>
                          </div>
                          <p className="text-xs text-gray-500 mt-1">{intg.pattern}</p>
                          {intg.data_flow && (
                            <p className="text-xs text-gray-600 mt-0.5">{intg.data_flow}</p>
                          )}
                          {intg.shared_interfaces && intg.shared_interfaces.length > 0 && (
                            <div className="flex flex-wrap gap-1 mt-1">
                              {intg.shared_interfaces.map((si, j) => (
                                <span key={j} className="px-1.5 py-0.5 bg-indigo-500/10 border border-indigo-500/20 rounded text-[10px] text-indigo-400">{si}</span>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {linkingDocument.shared_types && linkingDocument.shared_types.length > 0 && (
                  <div className="px-4 py-3">
                    <span className="text-[11px] text-gray-600 uppercase tracking-wider">Shared Types</span>
                    <div className="flex flex-wrap gap-1.5 mt-1.5">
                      {linkingDocument.shared_types.map((t, i) => (
                        <span key={i} className="px-2 py-0.5 bg-indigo-500/10 border border-indigo-500/20 rounded text-[10px] text-indigo-400 font-mono">{t}</span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Index metadata */}
          {indexMetadata && indexMetadata.deps && Object.keys(indexMetadata.deps).length > 0 && (
            <div>
              <span className="text-[11px] text-gray-600 uppercase tracking-wider">Search Indexes</span>
              <div className="mt-1.5 bg-gray-900 border border-gray-800 rounded-lg px-4 py-3">
                <div className="space-y-1">
                  <div className="flex items-center gap-2 text-xs">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 shrink-0" />
                    <span className="text-gray-400">Main repo</span>
                    <span className="text-gray-600 ml-auto">indexed</span>
                  </div>
                  {Object.entries(indexMetadata.deps).map(([name, meta]) => (
                    <div key={name} className="flex items-center gap-2 text-xs">
                      <span className={`w-1.5 h-1.5 rounded-full ${meta.indexed ? 'bg-emerald-400' : 'bg-gray-600'} shrink-0`} />
                      <span className="text-gray-400 font-mono">{name}</span>
                      <span className="text-gray-600 ml-auto">{meta.indexed ? 'indexed' : 'pending'}</span>
                    </div>
                  ))}
                </div>
                <p className="text-[11px] text-gray-600 mt-2">Semantic search covers all indexed repos.</p>
              </div>
            </div>
          )}
        </div>
      )}
    </>
  )
}
