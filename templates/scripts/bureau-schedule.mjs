// Pure conflict partition shared by the CLI and installed Claude workflow.
export function buildSchedule(tickets, predictions) {
  if (!Array.isArray(tickets) || !Array.isArray(predictions)) throw Error('Expected ticket and prediction arrays');
  const ids = tickets.map(t => t.ticket);
  if (ids.some(id => typeof id !== 'string' || !/^[A-Z][A-Z0-9]*-[0-9]+$/.test(id)) || new Set(ids).size !== ids.length)
    throw Error('Ticket identifiers must be valid and unique');
  const normalize = p => {
    if (typeof p !== 'string') throw Error('Non-string path');
    p = p.trim().replace(/^(\.\/)+/, '').replace(/\/+$/, '');
    if (!p || p.startsWith('/') || /[\\:*?\[\]{}]/.test(p) || p.split('/').some(x => !x || x === '..' || x === '.'))
      throw Error('Expected concrete repository-relative path');
    return p;
  };
  const blocked = [];
  const radii = tickets.map(t => {
    const matches = predictions.filter(p => p && p.ticket === t.ticket);
    try {
      if (matches.length !== 1 || matches[0].error || !Array.isArray(matches[0].predicted_paths) || !matches[0].predicted_paths.length)
        throw Error('Missing, failed, duplicate or empty prediction');
      return {...t, predicted_paths: matches[0].predicted_paths.map(normalize)};
    } catch (error) {
      blocked.push({ticket: t.ticket, reason: error.message});
      return {...t, predicted_paths: null};
    }
  });
  for (const p of predictions) if (p && !ids.includes(p.ticket)) throw Error('Unexpected prediction ticket');
  const parent = ids.map((_, i) => i), edges = [];
  const find = i => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
  for (let i = 0; i < radii.length; i++) for (let j = i + 1; j < radii.length; j++) {
    const a = radii[i], b = radii[j];
    const overlap = !a.predicted_paths || !b.predicted_paths ? 'unknown blast radius' :
      a.predicted_paths.find(p => b.predicted_paths.some(q => p === q || p.startsWith(q + '/') || q.startsWith(p + '/')));
    if (overlap) { edges.push({a: a.ticket, b: b.ticket, why: overlap}); parent[find(i)] = find(j); }
  }
  const components = new Map();
  ids.forEach((id, i) => { const root = find(i); if (!components.has(root)) components.set(root, []); components.get(root).push(id); });
  const groups = [...components.values()];
  const parallelSafe = groups.filter(g => g.length === 1).flat();
  const serialChains = groups.filter(g => g.length > 1);
  const esc = x => String(x).replace(/\|/g, '\\|').replace(/\n/g, ' ');
  const table = '| Ticket | Predicted paths |\n|---|---|\n' + radii.map(r => `| ${r.ticket} | ${esc(r.predicted_paths?.join(', ') ?? 'UNKNOWN — blocked')} |`).join('\n');
  return {table, parallelSafe, serialChains, edges, blocked};
}
