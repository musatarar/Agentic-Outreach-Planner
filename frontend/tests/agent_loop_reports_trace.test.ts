/**
 * Reports-page agent trace (MUS-29) — planted as todo by the skeleton PR.
 * The reports_trace component PR replaces these with real assertions on the
 * trace-rendering helper (llm_call → assistant text or "requested: {tools}",
 * tool_result → capped string in a <pre>, final → the draft; 404 hides the
 * toggle entirely).
 */
import { test } from 'node:test';

test('renders llm_call, tool_result and final steps as a numbered list', { todo: true });
test('shows "requested: {tool names}" for a text-less llm_call step', { todo: true });
test('hides the trace toggle when the endpoint 404s (single-shot action)', { todo: true });
