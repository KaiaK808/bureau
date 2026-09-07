#!/usr/bin/env node
// Input: {tickets:[{ticket,title,summary?}], predictions:[{ticket,predicted_paths,...}|null]}.
import {readFileSync} from 'node:fs';
import {buildSchedule} from './bureau-schedule.mjs';
try {
  const input = JSON.parse(readFileSync(process.argv[2] || 0, 'utf8'));
  const schedule = buildSchedule(input.tickets, input.predictions);
  process.stdout.write(JSON.stringify(schedule, null, 2) + '\n');
  if (schedule.blocked.length) process.exitCode = 25;
} catch (error) { console.error(error.message); process.exitCode = 1; }
