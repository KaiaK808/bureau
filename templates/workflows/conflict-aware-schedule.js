export const meta = {
  name: 'conflict-aware-schedule',
  description: 'Predict each ticket blast radius, build a conflict graph, partition into parallel-safe sets + serial chains for orchestrate.sh --execute',
  phases: [
    { title: 'BlastRadius', detail: 'one agent per ticket predicts the files it will touch' },
  ],
}

// Backlog tickets to schedule: [{ ticket, title, summary }]. Pass as the
// workflow's `args` (array, or a JSON string of one). The returned
// { parallelSafe, serialChains } is exactly what
// `orchestrate.sh --execute --schedule <file>` consumes — save the return
// value to schedule.json and run the executor over it.
const tickets = typeof args === 'string' ? JSON.parse(args) : args

const BLAST_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    ticket: { type: 'string' },
    title: { type: 'string' },
    primary_areas: { type: 'array', items: { type: 'string' }, description: 'top-level dirs/packages/modules the ticket touches' },
    predicted_paths: { type: 'array', items: { type: 'string' }, description: 'concrete repo-relative files most likely edited' },
    rationale: { type: 'string' },
  },
  required: ['ticket', 'title', 'primary_areas', 'predicted_paths', 'rationale'],
}

function blastPrompt(t) {
  return `You are predicting the BLAST RADIUS of a backlog ticket for conflict-aware scheduling of THIS repository. Two tickets that touch overlapping files cannot be built in parallel (they would collide on rebase/merge); disjoint ones can.

Ticket: ${t.ticket} — ${t.title}
Summary: ${t.summary || ''}

Method (read-only): grep/glob THIS repository for the modules, files, and symbols this ticket would edit. Map the ticket's intent to concrete locations (e.g. an auth change → the auth module + its tests; a new CLI flag → the arg parser; a renderer tweak → the render/view layer). Confirm each path exists before listing it.

Return: primary_areas (the top-level dirs/packages/modules touched) and predicted_paths (concrete repo-relative files, e.g. src/auth/login.ts). Be concrete and grounded in what you actually grep — over-listing files causes false serialization, under-listing causes real conflicts, so aim for the true edit set.`
}

phase('BlastRadius')
log(`Predicting blast radius for ${tickets.length} tickets...`)

const radii = (await parallel(tickets.map(t => () =>
  agent(blastPrompt(t), { label: `blast:${t.ticket}`, phase: 'BlastRadius', schema: BLAST_SCHEMA })
))).filter(Boolean)

// Deterministic conflict graph + partition (union-find). Two tickets conflict
// when their predicted file sets overlap — pure file-overlap, repo-agnostic.
const norm = p => String(p).trim().replace(/^\.?\/*/, '')
const setOf = r => new Set((r.predicted_paths || []).map(norm))
const conflict = (a, b) => {
  const A = setOf(a), B = setOf(b)
  for (const p of A) if (B.has(p)) return `shared file ${p}`
  return null
}
const n = radii.length
const parent = radii.map((_, i) => i)
const find = i => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i] } return i }
const union = (i, j) => { parent[find(i)] = find(j) }
const edges = []
for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
  const why = conflict(radii[i], radii[j])
  if (why) { edges.push({ a: radii[i].ticket, b: radii[j].ticket, why }); union(i, j) }
}
const comps = {}
for (let i = 0; i < n; i++) { const r = find(i); (comps[r] = comps[r] || []).push(radii[i]) }
const groups = Object.values(comps)
const parallelSafe = groups.filter(g => g.length === 1).map(g => g[0])
const serialChains = groups.filter(g => g.length > 1)

const esc = s => String(s || '').replace(/\|/g, '\\|').replace(/\n/g, ' ').trim()
let md = `## Conflict-aware schedule — ${n} tickets\n\n`
md += `### Blast radii\n\n| Ticket | Areas | Predicted paths |\n|---|---|---|\n`
for (const r of radii) md += `| ${r.ticket} | ${esc((r.primary_areas || []).join(', '))} | ${esc((r.predicted_paths || []).join(', '))} |\n`
md += `\n### Conflict edges (${edges.length})\n\n`
md += edges.length ? edges.map(e => `- **${e.a} ↔ ${e.b}** — ${esc(e.why)}`).join('\n') : '- none — fully parallelizable'
md += `\n\n### Schedule\n\n`
md += `**Parallel-safe (run concurrently, separate worktrees):** ${parallelSafe.length ? parallelSafe.map(r => r.ticket).join(', ') : '(none)'}\n\n`
if (serialChains.length) {
  md += `**Must serialize (conflict components — one shepherd at a time):**\n`
  for (const c of serialChains) md += `- chain: ${c.map(r => r.ticket).join(' → ')}\n`
} else {
  md += `**Must serialize:** (none — all disjoint)\n`
}

return { table: md, parallelSafe: parallelSafe.map(r => r.ticket), serialChains: serialChains.map(c => c.map(r => r.ticket)), edges }
