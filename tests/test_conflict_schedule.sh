#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
node --input-type=module <<'JS'
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {buildSchedule} from './templates/scripts/bureau-schedule.mjs';
const tickets = [1,2,3,4].map(n => ({ticket:`T-${n}`}));
const predictions = ['src/a','src/b','./src/a/file','src/d'].map((p,i) => ({ticket:`T-${i+1}`,predicted_paths:[p]}));
const result = buildSchedule(tickets,predictions);
assert.deepEqual(result.parallelSafe,['T-2','T-4']);
assert.deepEqual(result.serialChains,[['T-1','T-3']]);
for (const bad of [null, {ticket:'T-2',predicted_paths:[]}, {ticket:'T-2',predicted_paths:['../secret']}]) {
  const partial = buildSchedule(tickets,[predictions[0],bad,...predictions.slice(2)]);
  assert.equal(partial.blocked[0].ticket,'T-2');
  assert.deepEqual(partial.serialChains,[tickets.map(t => t.ticket)]);
}
assert.throws(() => buildSchedule([tickets[0],tickets[0]],[]));
// Execute the workflow using the exact same insertion the installer performs.
const core = fs.readFileSync('templates/scripts/bureau-schedule.mjs','utf8').replace('export function','function');
const source = fs.readFileSync('templates/workflows/conflict-aware-schedule.js','utf8').replace('export const meta','const meta').replace('/* BUREAU_SCHEDULER_CORE */',core);
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
let n=0;
const emitted = await new AsyncFunction('args','phase','log','parallel','agent',source)(tickets,()=>{},()=>{},jobs=>Promise.all(jobs.map(j=>j())),async()=>predictions[n++]);
assert.deepEqual(emitted,result);
console.log('PASS actual shared core and emitted workflow');
JS
