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

const radii = (await parallel(tickets.map(t => async () => {
  try { return await agent(blastPrompt(t), { label: `blast:${t.ticket}`, phase: 'BlastRadius', schema: BLAST_SCHEMA }) }
  catch (error) { return {ticket: t.ticket, error: String(error)} }
})))

/* BUREAU_SCHEDULER_CORE */
return buildSchedule(tickets, radii)
