#!/bin/bash
# test_conflict_schedule.sh — the conflict-aware-schedule workflow brain.
# Structural + generalization checks run always (bash/jq only). The union-find
# partition (the deterministic core; the LLM blast-radius half isn't
# unit-testable) runs under node when available, skips cleanly otherwise — the
# workflow file itself can't be `node --check`'d (the Workflow runtime wraps it,
# so its top-level `return`/`export` aren't standalone-valid).

set -uo pipefail
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
WF="$TESTS_DIR/../templates/workflows/conflict-aware-schedule.js"

fail=0
check() { if eval "$2"; then echo "ok   - $1"; else echo "FAIL - $1"; fail=1; fi; }

# ── structure ──────────────────────────────────────────────────────────────
check "workflow file present" '[ -f "$WF" ]'
check "names the scheduler brain" 'grep -q "name: .conflict-aware-schedule." "$WF"'
check "has the BlastRadius phase" "grep -q \"phase('BlastRadius')\" \"\$WF\""
check "emits parallelSafe + serialChains (the executor's input)" \
  'grep -q "parallelSafe" "$WF" && grep -q "serialChains" "$WF"'

# ── generalization: fork-isms must be gone (this is a generic template) ──────
check "no hardcoded fork path" '! grep -q "/Users/" "$WF"'
check "no main.rs conflict-magnet special-case" '! grep -qiE "main\.rs|touches_main_rs" "$WF"'
check "no brainhuggers/Rust-isms" '! grep -qiE "brainhuggers|claw-cli|crates/" "$WF"'

# ── partition algorithm (node-gated) ─────────────────────────────────────────
if command -v node >/dev/null 2>&1; then
  if node -e '
    // Mirrors the workflow union-find: A & C share src/a.ts → one serial chain;
    // B and D are disjoint → parallel-safe.
    const radii = [
      { ticket: "A", predicted_paths: ["src/a.ts"] },
      { ticket: "B", predicted_paths: ["src/b.ts"] },
      { ticket: "C", predicted_paths: ["./src/a.ts", "src/c.ts"] },
      { ticket: "D", predicted_paths: ["src/d.ts"] },
    ];
    const norm = p => String(p).trim().replace(/^\.?\/*/, "");
    const setOf = r => new Set((r.predicted_paths||[]).map(norm));
    const conflict = (a,b) => { const A=setOf(a),B=setOf(b); for (const p of A) if (B.has(p)) return p; return null; };
    const n = radii.length, parent = radii.map((_,i)=>i);
    const find = i => { while(parent[i]!==i){parent[i]=parent[parent[i]];i=parent[i];} return i; };
    const union = (i,j) => { parent[find(i)]=find(j); };
    for (let i=0;i<n;i++) for (let j=i+1;j<n;j++) if (conflict(radii[i],radii[j])) union(i,j);
    const comps={}; for (let i=0;i<n;i++){const r=find(i);(comps[r]=comps[r]||[]).push(radii[i].ticket);}
    const groups=Object.values(comps);
    const parallelSafe=groups.filter(g=>g.length===1).map(g=>g[0]).sort();
    const serialChains=groups.filter(g=>g.length>1).map(g=>g.slice().sort());
    const ok = JSON.stringify(parallelSafe)===JSON.stringify(["B","D"])
      && serialChains.length===1
      && JSON.stringify(serialChains[0])===JSON.stringify(["A","C"]);
    if (!ok) { console.error("partition mismatch: "+JSON.stringify({parallelSafe,serialChains})); process.exit(1); }
  '; then
    echo "ok   - partition: A↔C (shared src/a.ts) serialize, B/D parallel-safe"
  else
    echo "FAIL - partition algorithm"; fail=1
  fi
else
  echo "skip - partition algorithm (node not available)"
fi

[ "$fail" -eq 0 ] && echo "PASS — conflict-aware-schedule brain"
exit "$fail"
