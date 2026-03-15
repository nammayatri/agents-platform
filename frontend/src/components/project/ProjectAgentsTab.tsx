const DEFAULT_AGENTS = [
  { role: 'coder', label: 'Coder', desc: 'Writes code, implements features, fixes bugs' },
  { role: 'tester', label: 'Tester', desc: 'Writes and runs tests, validates implementations' },
  { role: 'reviewer', label: 'Reviewer', desc: 'Reviews code quality, suggests improvements' },
  { role: 'pr_creator', label: 'PR Creator', desc: 'Creates pull requests with proper descriptions' },
  { role: 'report_writer', label: 'Report Writer', desc: 'Generates documentation and reports' },
  { role: 'merge_agent', label: 'Merge Agent', desc: 'Merges approved PRs, checks CI, runs post-merge builds' },
]

export default function ProjectAgentsTab() {
  return (
    <>
      <div>
        <p className="text-sm text-gray-300">Default Agent Team</p>
        <p className="text-[11px] text-gray-600 mt-0.5">These specialist agents work together to complete tasks for this project.</p>
      </div>
      <div className="space-y-1.5">
        {DEFAULT_AGENTS.map((a) => (
          <div key={a.role} className="flex items-center gap-3 px-3 py-2.5 bg-gray-900 border border-gray-800 rounded-lg">
            <div className="w-7 h-7 rounded-full bg-indigo-500/10 flex items-center justify-center text-[11px] font-semibold text-indigo-400 shrink-0">
              {a.label.charAt(0)}
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm text-white">{a.label}</p>
              <p className="text-xs text-gray-500">{a.desc}</p>
            </div>
            <span className="px-2 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500 font-mono">{a.role}</span>
          </div>
        ))}
      </div>
      <div className="pt-2">
        <div className="py-4 text-center text-xs text-gray-600 border border-dashed border-gray-800 rounded-lg">
          Custom agent teams coming soon. Configure agents globally in the Agents page.
        </div>
      </div>
    </>
  )
}
