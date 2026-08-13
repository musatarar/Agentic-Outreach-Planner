import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { errorMessage } from '../api/client';
import { fetchOutreachTrace, fetchReports } from '../api/endpoints';
import type { AgentTrace, Lead, OutreachAction } from '../api/types';
import { EmptyState, ErrorMessage } from '../components/Messages';
import { PageHeader } from '../components/PageHeader';
import { PriorityBadge } from '../components/PriorityBadge';
import { formatActionType, formatTimestamp } from '../util/labels';
import { hidesTraceToggle, traceStepViews } from '../util/trace';

interface LeadGroup {
  lead: Lead;
  entries: OutreachAction[];
}

/** Group by lead, preserving the API's newest-first order within each group. */
function groupByLead(actions: OutreachAction[]): LeadGroup[] {
  const groups = new Map<number, LeadGroup>();
  for (const action of actions) {
    const group = groups.get(action.lead.id);
    if (group) {
      group.entries.push(action);
    } else {
      groups.set(action.lead.id, { lead: action.lead, entries: [action] });
    }
  }
  return [...groups.values()];
}

type TraceState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'loaded'; trace: AgentTrace }
  | { phase: 'error'; message: string }
  | { phase: 'hidden' }; // 404: single-shot action, no agent run

/**
 * Collapsible per-entry agent step log. Lazy: nothing is fetched until the
 * first expand, and a `no_agent_trace` 404 removes the toggle entirely —
 * single-shot rows simply have no trace to show.
 */
function TraceSection({ actionId }: { actionId: number }) {
  const [state, setState] = useState<TraceState>({ phase: 'idle' });

  if (state.phase === 'hidden') return null;

  const load = () => {
    if (state.phase !== 'idle') return;
    setState({ phase: 'loading' });
    fetchOutreachTrace(actionId)
      .then((trace) => setState({ phase: 'loaded', trace }))
      .catch((err: unknown) => {
        setState(
          hidesTraceToggle(err)
            ? { phase: 'hidden' }
            : { phase: 'error', message: errorMessage(err) },
        );
      });
  };

  return (
    <details
      className="copy-block"
      onToggle={(event) => {
        if ((event.target as HTMLDetailsElement).open) load();
      }}
    >
      <summary>How this draft was reached</summary>
      {state.phase === 'loading' && <div className="bd-note">Loading trace…</div>}
      {state.phase === 'error' && (
        <div className="bd-note">Failed to load trace: {state.message}</div>
      )}
      {state.phase === 'loaded' && (
        <>
          <div className="bd-note">
            Run {state.trace.status} · {state.trace.steps_used} steps ·{' '}
            {state.trace.tool_calls_used} tool calls
          </div>
          <ol className="trace-steps">
            {traceStepViews(state.trace.steps).map((view) => (
              <li key={view.seq}>
                <div className="section-label">{view.title}</div>
                {view.text !== '' &&
                  (view.pre ? (
                    <div className="copy-pre">{view.text}</div>
                  ) : (
                    <div>{view.text}</div>
                  ))}
              </li>
            ))}
          </ol>
        </>
      )}
    </details>
  );
}

function Entry({ entry }: { entry: OutreachAction }) {
  return (
    <div className="entry">
      <div className="entry-meta">
        <PriorityBadge priority={entry.priority} />
        <span className="action-label">{formatActionType(entry.action_type)}</span>
        <span className="timestamp">{formatTimestamp(entry.created_at)}</span>
      </div>

      <div className="section-label">Issue</div>
      <div className="reason">{entry.reason}</div>

      <div className="section-label">How it was handled</div>
      {entry.needs_human ? (
        <div className="bd-note">Reported to BD — no copy sent automatically.</div>
      ) : entry.suggested_copy ? (
        <details className="copy-block">
          <summary>Show suggested copy</summary>
          <div className="copy-pre">{entry.suggested_copy}</div>
        </details>
      ) : (
        <div className="bd-note">No copy generated.</div>
      )}

      <TraceSection actionId={entry.id} />

      {entry.further_action && (
        <div className="next-action">
          <strong>Next action:</strong> {entry.further_action}
        </div>
      )}
    </div>
  );
}

export function ReportsPage() {
  const [groups, setGroups] = useState<LeadGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    fetchReports()
      .then((actions) => {
        if (active) setGroups(groupByLead(actions));
      })
      .catch((err: unknown) => {
        if (active) setError(errorMessage(err));
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <>
      <PageHeader
        current="/reports/"
        title="Outreach Reports"
        subtitle="Per-lead audit trail: every issue detected and how it was handled"
      />

      <div className="container medium">
        {error ? (
          <ErrorMessage>Failed to load reports: {error}</ErrorMessage>
        ) : groups === null ? (
          <EmptyState>Loading reports…</EmptyState>
        ) : groups.length === 0 ? (
          <EmptyState>
            No outreach reports yet. Run the planner from the{' '}
            <Link to="/">Planner</Link> page.
          </EmptyState>
        ) : (
          groups.map(({ lead, entries }) => (
            <div className="lead-group" key={lead.id}>
              <div className="lead-header">
                <h2>{lead.agency_name}</h2>
                <div className="contact">
                  {lead.contact_name} &middot; {lead.contact_email}
                </div>
              </div>
              {entries.map((entry) => (
                <Entry key={entry.id} entry={entry} />
              ))}
            </div>
          ))
        )}
      </div>
    </>
  );
}
