import { useState } from 'react'
import { projects as projectsApi } from '../../services/api'

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
  setError: (err: string) => void
}

export default function ProjectUnderstandingTab({
  projectId, repoUrl, analysisStatus, projectUnderstanding, setAnalysisStatus, setError,
}: ProjectUnderstandingTabProps) {
  const [analyzing, setAnalyzing] = useState(false)

  return (
    <>
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-600">AI-generated understanding of the project from its repository.</p>
        {repoUrl && (
          <button
            onClick={async () => {
              if (!projectId) return
              setAnalyzing(true)
              try { await projectsApi.analyze(projectId); setAnalysisStatus('analyzing') }
              catch (e) { setError(e instanceof Error ? e.message : 'Analysis failed') }
              finally { setAnalyzing(false) }
            }}
            disabled={analyzing || analysisStatus === 'analyzing'}
            className="text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-40 transition-colors shrink-0"
          >
            {analyzing ? 'Starting...' : analysisStatus === 'complete' ? 'Re-analyze' : 'Analyze'}
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

      {analysisStatus === 'analyzing' && (
        <div className="px-4 py-3 bg-indigo-500/5 border border-indigo-500/10 rounded-lg">
          <div className="flex items-center gap-2">
            <svg className="w-3 h-3 animate-spin text-indigo-400" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
              <path className="opacity-80" d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
            </svg>
            <span className="text-sm text-indigo-300/80">Analyzing repository...</span>
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
