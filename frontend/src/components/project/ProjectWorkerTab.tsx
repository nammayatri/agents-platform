import { useState, useEffect } from 'react'
import { projects as projectsApi } from '../../services/api'

interface Props {
  projectId: string
  setError: (msg: string) => void
}

export default function ProjectWorkerTab({ projectId, setError }: Props) {
  const [image, setImage] = useState('')
  const [pvcSizeGb, setPvcSizeGb] = useState('20')
  const [bootScript, setBootScript] = useState('')
  const [nodeType, setNodeType] = useState('')
  const [cpuRequest, setCpuRequest] = useState('500m')
  const [memRequest, setMemRequest] = useState('1Gi')
  const [cpuLimit, setCpuLimit] = useState('4')
  const [memLimit, setMemLimit] = useState('8Gi')
  const [advancedMode, setAdvancedMode] = useState(false)
  const [podSpecJson, setPodSpecJson] = useState('')
  const [saving, setSaving] = useState(false)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    projectsApi.get(projectId).then((p) => {
      const s = p.settings_json?.worker || {}
      setImage(s.image || '')
      setPvcSizeGb(String(s.pvc_size_gb || 20))
      setBootScript(s.boot_script || '')
      setNodeType(s.node_type || '')
      const r = s.resources || {}
      setCpuRequest(r.cpu_request || '500m')
      setMemRequest(r.memory_request || '1Gi')
      setCpuLimit(r.cpu_limit || '4')
      setMemLimit(r.memory_limit || '8Gi')
      if (s.pod_spec_override) {
        setAdvancedMode(true)
        setPodSpecJson(JSON.stringify(s.pod_spec_override, null, 2))
      }
      setLoaded(true)
    }).catch(() => setError('Failed to load worker settings'))
  }, [projectId])

  const handleSave = async () => {
    setSaving(true)
    setError('')
    try {
      let podSpecOverride = null
      if (advancedMode && podSpecJson.trim()) {
        try {
          podSpecOverride = JSON.parse(podSpecJson)
        } catch {
          setError('Invalid JSON in pod spec override')
          setSaving(false)
          return
        }
      }

      await projectsApi.updateSettingsSection(projectId, 'worker', {
        image: image || null,
        pvc_size_gb: parseInt(pvcSizeGb) || 20,
        boot_script: bootScript || null,
        node_type: nodeType || null,
        resources: {
          cpu_request: cpuRequest,
          memory_request: memRequest,
          cpu_limit: cpuLimit,
          memory_limit: memLimit,
        },
        pod_spec_override: podSpecOverride,
      })
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  if (!loaded) {
    return <div className="animate-pulse bg-gray-800 rounded h-40" />
  }

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm text-gray-300 mb-1">Worker Pod Settings</h3>
        <p className="text-[11px] text-gray-600">
          Each task spawns a dedicated pod with its own PVC for workspace isolation.
        </p>
      </div>

      <div className="space-y-4">
        <div>
          <label className="text-xs text-gray-500 block mb-1.5">Container Image</label>
          <input
            type="text"
            value={image}
            onChange={(e) => setImage(e.target.value)}
            placeholder="Leave empty to use default backend image"
            className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors"
          />
          <p className="text-[11px] text-gray-600 mt-1">
            Custom image for task pods. Must have the agents package installed.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="text-xs text-gray-500 block mb-1.5">PVC Size (GB)</label>
            <input
              type="number"
              value={pvcSizeGb}
              onChange={(e) => setPvcSizeGb(e.target.value)}
              min="1"
              max="500"
              className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors"
            />
            <p className="text-[11px] text-gray-600 mt-1">Default: 20 GB.</p>
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1.5">Node Type</label>
            <input
              type="text"
              value={nodeType}
              onChange={(e) => setNodeType(e.target.value)}
              placeholder="e.g. m5.xlarge"
              className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors"
            />
            <p className="text-[11px] text-gray-600 mt-1">
              Schedules pods on nodes with label <span className="font-mono">nodeType=&lt;value&gt;</span>. Empty = any node.
            </p>
          </div>
        </div>

        {/* Resource Limits */}
        <div>
          <label className="text-xs text-gray-500 block mb-2">Resource Limits</label>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-[11px] text-gray-600 block mb-1">CPU Request</label>
              <input
                type="text"
                value={cpuRequest}
                onChange={(e) => setCpuRequest(e.target.value)}
                placeholder="500m"
                className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors"
              />
            </div>
            <div>
              <label className="text-[11px] text-gray-600 block mb-1">CPU Limit</label>
              <input
                type="text"
                value={cpuLimit}
                onChange={(e) => setCpuLimit(e.target.value)}
                placeholder="4"
                className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors"
              />
            </div>
            <div>
              <label className="text-[11px] text-gray-600 block mb-1">Memory Request</label>
              <input
                type="text"
                value={memRequest}
                onChange={(e) => setMemRequest(e.target.value)}
                placeholder="1Gi"
                className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors"
              />
            </div>
            <div>
              <label className="text-[11px] text-gray-600 block mb-1">Memory Limit</label>
              <input
                type="text"
                value={memLimit}
                onChange={(e) => setMemLimit(e.target.value)}
                placeholder="8Gi"
                className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors"
              />
            </div>
          </div>
          <p className="text-[11px] text-gray-600 mt-1.5">
            Standard K8s notation: 500m, 1, 2 for CPU; 512Mi, 1Gi, 8Gi for memory.
          </p>
        </div>

        <div>
          <label className="text-xs text-gray-500 block mb-1.5">Boot Script</label>
          <textarea
            value={bootScript}
            onChange={(e) => setBootScript(e.target.value)}
            rows={4}
            placeholder="#!/bin/sh&#10;# Commands to run before task execution starts"
            className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm font-mono focus:outline-none focus:border-indigo-500 transition-colors"
          />
          <p className="text-[11px] text-gray-600 mt-1">
            Shell script executed on pod startup. Max timeout: 5 minutes.
          </p>
        </div>

        {/* Advanced Mode Toggle */}
        <div className="pt-2 border-t border-gray-800">
          <button
            type="button"
            onClick={() => setAdvancedMode(!advancedMode)}
            className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
          >
            {advancedMode ? '▾ Hide Advanced Pod Spec' : '▸ Advanced: Custom Pod Spec'}
          </button>
        </div>

        {advancedMode && (
          <div>
            <label className="text-xs text-gray-500 block mb-1.5">Pod Spec Override (JSON)</label>
            <textarea
              value={podSpecJson}
              onChange={(e) => setPodSpecJson(e.target.value)}
              rows={12}
              placeholder={`{
  "spec": {
    "tolerations": [...],
    "nodeSelector": {"gpu": "true"},
    "containers": [{
      "resources": {...},
      "env": [{"name": "CUSTOM", "value": "val"}]
    }]
  }
}`}
              className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm font-mono focus:outline-none focus:border-indigo-500 transition-colors"
            />
            <p className="text-[11px] text-gray-600 mt-1">
              Raw K8s pod spec. The system will inject required fields (name, labels, PVC volume,
              worker command, env vars). Your tolerations, node selectors, extra volumes, sidecars,
              and resource overrides take precedence. Leave empty to use standard settings above.
            </p>
          </div>
        )}
      </div>

      <div className="pt-4 border-t border-gray-800">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
        >
          {saving ? 'Saving...' : 'Save Worker Settings'}
        </button>
      </div>
    </div>
  )
}
