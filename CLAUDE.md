# Agent Platform — UI/UX Rulebook

Design guidelines and conventions for the Agent Platform frontend. Reference this when building new features or modifying existing UI.

---

## Design System

### Color Palette (Tailwind classes)
- **Background**: `bg-gray-950` (main), `bg-gray-900` (cards/surfaces)
- **Borders**: `border-gray-900` (primary), `border-gray-800` (card borders)
- **Text**: `text-white` (primary), `text-gray-300` (secondary), `text-gray-500` (muted), `text-gray-600` (hint/label)
- **Accent**: `indigo-500/400` (primary actions, active states), `amber-500/400` (plan mode), `emerald-500/400` (success), `red-500/400` (error/danger)
- **Interactive hover**: `hover:bg-gray-900/50` (list items), `hover:border-gray-700` (cards)

### Typography
- Page titles: `text-xl font-semibold text-white`
- Section headers: `text-sm text-gray-300`
- Labels: `text-xs text-gray-500` or `text-[11px] text-gray-600 uppercase tracking-wider`
- Body text: `text-sm text-gray-400`
- Hint text: `text-[11px] text-gray-600`
- Monospace/code: `font-mono text-xs`

### Spacing
- Page padding: `p-6`
- Card padding: `px-4 py-3` or `px-3 py-2.5`
- Section spacing: `space-y-6` between sections, `space-y-1.5` between list items
- Border radius: `rounded-lg` (default), `rounded-xl` (inputs/buttons in chat), `rounded-full` (avatars/dots)

### Components

**Buttons**
- Primary: `px-5 py-2 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-sm font-medium text-white`
- Secondary: `px-5 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300`
- Danger (text): `text-sm text-red-400 hover:text-red-300`
- Disabled: `disabled:opacity-40` or `disabled:opacity-50`

**Inputs**
- Standard: `w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors`
- Select: same as input

**Cards/List items**
- `px-3 py-2.5 bg-gray-900 border border-gray-800 rounded-lg`
- Hover: `hover:border-gray-700`
- Group hover actions: `hidden group-hover:flex`

**Badges/Tags**
- Status: `px-2 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500 font-medium`
- Active: `px-2 py-0.5 bg-indigo-500/10 border border-indigo-500/20 rounded text-[10px] text-indigo-400`

**Avatars**
- User: `w-6 h-6 rounded-full bg-gray-800` (small), `w-8 h-8 rounded-full` (medium)
- Project icon: `w-5 h-5 rounded object-cover` (sidebar), `w-8 h-8 rounded` (settings preview)
- Fallback: first character of name, centered

---

## Layout Rules

### Main Sidebar (AppShell)
- Fixed width: 240px (`w-60`)
- Locked open by default — users can unlock (unpin) to enable collapsible behavior
- When unlocked: collapses to 56px (`w-14`), expands on hover with 300ms ease transition, 250ms collapse delay
- Inner wrapper always renders at 240px — outer aside clips via `overflow-hidden` to prevent height shifts
- Text labels use opacity transition (`opacity-0`/`opacity-100`) independent of width transition
- Pin/lock button in header: locked = `text-indigo-400`, unlocked = `text-gray-600`
- **Do NOT conditionally render different elements for collapsed vs expanded** — always render the same DOM, toggle visibility with CSS

### Sub-sidebars (Page-level)
- Collapsible pattern is appropriate for sub-sidebars (e.g., chat sessions list in ProjectChatPage)
- These can toggle show/hide via a hamburger button
- Width: `w-56` typical for page sub-sidebars
- Only use collapse-on-hover for the main sidebar; sub-sidebars use explicit toggle buttons

### Content Areas
- Max width: `max-w-2xl mx-auto` (settings), `max-w-3xl mx-auto` (chat), `max-w-xl mx-auto` (wizard)
- Full height: `flex-1 overflow-y-auto`

---

## Interaction Patterns

### Transitions
- Width transitions: `transition-[width] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]`
- Opacity transitions: `transition-opacity duration-200`
- Color transitions: `transition-colors` (default duration)
- Avoid aggressive/fast transitions — minimum 200ms for meaningful animations, 300ms for layout changes

### Forms
- Inline editing: edit forms appear below the clicked item (not modals/overlays)
- Step wizard for complex creation flows (4 steps max)
- Tabbed interface for settings/edit pages
- Auto-save is not used — explicit Save button required

### Error Handling
- Inline errors: `px-4 py-2.5 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-sm`
- Field-level errors: `text-xs text-red-400 mt-1.5`
- No toast/snackbar system — errors are always inline near the action

### Loading States
- Skeleton: `animate-pulse` with `bg-gray-800 rounded` placeholder blocks
- Inline spinners: `animate-spin` SVG circle
- Button loading: text changes to "Saving..." / "Creating..." etc., button disabled

### Empty States
- Dashed border: `py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg`
- Include actionable hint text when possible

---

## Access Control (UI)

### Owner vs Member
- `user_role` field on Project: `'owner'` | `'member'`
- Owner-only actions: delete project, update settings, manage members, update enablement
- Member actions: view project, create todos, use chat, view deliverables
- Hide owner-only buttons for members (don't show disabled — just hide)
- Members tab: show "Add Member" form only for owners

---

## Chat UX

### Message Bubbles
- User: `bg-indigo-600 text-white` (right-aligned)
- Assistant: `bg-gray-900 border border-gray-800 text-gray-300` (left-aligned)
- System/Error: `bg-red-900/30 text-red-400 border border-red-800/50` (left-aligned)
- Max width: `max-w-[80%]`

### Chat Input
- Textarea with auto-resize (max 120px height)
- Enter sends, Shift+Enter for newline
- Send button color matches mode: indigo (chat), amber (plan mode)

### Thinking Indicator
- Pulsing "Thinking..." with bouncing dots

---

## Project Icons
- Optional `icon_url` field on projects
- Displayed in sidebar: fixed `w-5 h-5` container with `bg-gray-800/60 rounded`, image `w-4 h-4 object-contain` inside — no border, subtle background for dark icon visibility
- Falls back to colored dot (`w-2 h-2 rounded-full`) if no icon or image fails to load
- Preview in settings form: `w-8 h-8` container, same pattern (no border)

---

## Development Rules

### Full-Stack Changes Are Mandatory
**Every feature must be implemented across all layers in a single pass:**
1. **Database migration** — new/altered columns or tables go in a numbered `.sql` file under `src/agents/db/migrations/`
2. **Backend API** — Pydantic models, route handlers, SQL queries updated to use the new schema
3. **Frontend types** — TypeScript interfaces in `types/index.ts` updated
4. **Frontend API** — Service methods in `services/api.ts` updated
5. **Frontend UI** — Components updated to use the new data
6. **Migration applied** — Run the migration against the local database before considering it done

Never ship a frontend change that references a column/table/endpoint that doesn't exist in the backend yet. Never ship a backend change without updating the frontend to use it.

### Migration Discipline
- Migrations are numbered sequentially: `NNN_description.sql` in `src/agents/db/migrations/`
- **Auto-applied on startup**: `_run_migrations()` in `connection.py` tracks applied files in the `_migrations` table and runs any new `.sql` files automatically when the server starts
- Use `IF NOT EXISTS` / `IF EXISTS` guards for idempotency (in case of manual apply or partial runs)
- Check existing schema before writing migration to avoid duplicates

---

## Backend Conventions (for context)

### LLM Response Parsing
- Never let parse failures crash the flow
- Retry up to 3 times with a correction prompt on JSON parse failure
- Remove the malformed response from conversation context before retrying
- Use `_extract_json()` to handle markdown fences and brace-depth matching
- Apply trailing-comma regex fix as second attempt before retry

### Access Checks
- Centralized in `deps.py`: `check_project_access()` (any member) and `check_project_owner()` (owner-only)
- All route handlers use these instead of inline checks
