import { useEffect, useState, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, GitMerge, Loader2, XCircle, CheckCircle2, Clock, Rocket, AlertTriangle } from 'lucide-react'
import { projects as projectsApi } from '../services/api'
import { usePipelineWebSocket } from '../hooks/usePipelineWebSocket'
import type { PipelineRun, PipelineRunStatus, Project } from '../types'

const statusConfig: Record<PipelineRunStatus, { label: string; color: string; Icon: typeof Clock }> = {
  pending:        { label: 'Pending',         color: 'text-gray-400 bg-gray-800',            Icon: Clock },
  testing:        { label: 'Testing',         color: 'text-blue-400 bg-blue-500/10',         Icon: Loader2 },
  test_passed:    { label: 'Tests Passed',    color: 'text-emerald-400 bg-emerald-500/10',   Icon: CheckCircle2 },
  test_failed:    { label: 'Tests Failed',    color: 'text-red-400 bg-red-500/10',           Icon: XCircle },
  deploying:      { label: 'Deploying',       color: 'text-amber-400 bg-amber-500/10',       Icon: Rocket },
  deploy_success: { label: 'Deployed',        color: 'text-emerald-400 bg-emerald-500/10',   Icon: CheckCircle2 },
  deploy_failed:  { label: 'Deploy Failed',   color: 'text-red-400 bg-red-500/10',           Icon: XCircle },
  skipped:        { label: 'Skipped',         color: 'text-gray-500 bg-gray-800',            Icon: Clock },
  cancelled:      { label: 'Cancelled',       color: 'text-gray-500 bg-gray-800',            Icon: AlertTriangle },
}

function StatusBadge({ status }: { status: PipelineRunStatus }) {
  const cfg = statusConfig[status] || statusConfig.pending
  const Icon = cfg.Icon
  const isAnimated = status === 'testing' || status === 'deploying' || status === 'pending'
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-medium ${cfg.color}`}>
      <Icon className={`w-3 h-3 ${isAnimated ? 'animate-spin' : ''}`} />
      {cfg.label}
    </span>
  )
}

function RunCard({ run, onCancel }: { run: PipelineRun; onCancel: (id: string) => void }) {
  const [expanded, setExpanded] = useState(false)
  const cancellable = ['pending', 'testing', 'deploying'].includes(run.status)
  const shortHash = run.commit_hash?.slice(0, 8) || ''

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg hover:border-gray-700 transition-colors">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 px-4 py-3 text-left"
      >
        <GitMerge className="w-4 h-4 text-indigo-400 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm text-white font-medium">#{run.pr_number}</span>
            {run.pr_title && (
              <span className="text-sm text-gray-400 truncate">{run.pr_title}</span>
            )}
          </div>
          <div className="flex items-center gap-3 mt-0.5 text-[11px] text-gray-600">
            <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500 font-mono">{run.repo_name}</span>
            {run.branch_name && <span className="font-mono">{run.branch_name}</span>}
            {shortHash && <span className="font-mono">{shortHash}</span>}
            <span>{new Date(run.created_at).toLocaleString()}</span>
          </div>
        </div>
        <StatusBadge status={run.status} />
        {cancellable && (
          <button
            onClick={(e) => { e.stopPropagation(); onCancel(run.id) }}
            className="text-xs text-gray-600 hover:text-red-400 transition-colors px-2 py-1"
          >
            Cancel
          </button>
        )}
      </button>

      {expanded && (
        <div className="px-4 pb-3 border-t border-gray-800/50 pt-2 space-y-2">
          {run.test_result && (
            <div>
              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Test Result</span>
              <pre className="mt-1 text-[11px] text-gray-500 font-mono bg-gray-950 rounded px-2 py-1.5 max-h-32 overflow-y-auto whitespace-pre-wrap">
                {typeof run.test_result.output === 'string'
                  ? run.test_result.output
                  : JSON.stringify(run.test_result.output, null, 2)}
              </pre>
            </div>
          )}
          {run.deploy_result && (
            <div>
              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Deploy Result</span>
              <pre className="mt-1 text-[11px] text-gray-500 font-mono bg-gray-950 rounded px-2 py-1.5 max-h-32 overflow-y-auto whitespace-pre-wrap">
                {JSON.stringify(run.deploy_result, null, 2)}
              </pre>
            </div>
          )}
          {run.webhook_token && run.status === 'testing' && (
            <div>
              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Webhook URL</span>
              <div className="mt-1 text-[11px] text-indigo-400 font-mono bg-gray-950 rounded px-2 py-1.5 break-all">
                POST /api/webhooks/pipeline-test/{run.webhook_token}
              </div>
            </div>
          )}
          <div className="flex gap-4 text-[10px] text-gray-600">
            {run.started_at && <span>Started: {new Date(run.started_at).toLocaleString()}</span>}
            {run.test_completed_at && <span>Tests: {new Date(run.test_completed_at).toLocaleString()}</span>}
            {run.deploy_completed_at && <span>Deploy: {new Date(run.deploy_completed_at).toLocaleString()}</span>}
          </div>
        </div>
      )}
    </div>
  )
}

export default function ProjectPipelinePage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const [project, setProject] = useState<Project | null>(null)
  const [runs, setRuns] = useState<PipelineRun[]>([])
  const [loading, setLoading] = useState(true)

  const { lastEvent } = usePipelineWebSocket(projectId || null)

  const loadRuns = useCallback(async () => {
    if (!projectId) return
    try {
      const data = await projectsApi.mergePipeline.listRuns(projectId)
      setRuns(data)
    } catch {
      // ignore
    }
  }, [projectId])

  useEffect(() => {
    if (!projectId) return
    projectsApi.get(projectId).then((p) => setProject(p as Project))
    loadRuns().finally(() => setLoading(false))
  }, [projectId, loadRuns])

  // Handle real-time updates
  useEffect(() => {
    if (!lastEvent || lastEvent.type === 'ping') return
    if (lastEvent.type === 'pipeline_status' && lastEvent.run_id) {
      setRuns((prev) =>
        prev.map((r) =>
          r.id === lastEvent.run_id ? { ...r, status: lastEvent.status! } : r
        )
      )
      // Reload for full data after terminal statuses
      if (['test_passed', 'test_failed', 'deploy_success', 'deploy_failed', 'cancelled'].includes(lastEvent.status || '')) {
        loadRuns()
      }
    }
  }, [lastEvent, loadRuns])

  const handleCancel = async (runId: string) => {
    if (!projectId) return
    try {
      await projectsApi.mergePipeline.cancelRun(projectId, runId)
      setRuns((prev) => prev.map((r) => r.id === runId ? { ...r, status: 'cancelled' as const } : r))
    } catch {
      // ignore
    }
  }

  if (!projectId || loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="flex items-center gap-2 text-gray-600 text-sm">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading...
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto p-6">
      <div className="max-w-3xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate(`/projects/${projectId}`)}
            className="text-gray-500 hover:text-gray-300 transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <h1 className="text-xl font-semibold text-white">Pipeline Runs</h1>
            <p className="text-sm text-gray-500">{project?.name || 'Project'}</p>
          </div>
        </div>

        {/* Runs list */}
        {runs.length === 0 ? (
          <div className="py-12 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
            No pipeline runs yet. Runs are triggered when PRs are merged.
          </div>
        ) : (
          <div className="space-y-2">
            {runs.map((run) => (
              <RunCard key={run.id} run={run} onCancel={handleCancel} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
