import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { errorMessage } from '../api/client';
import {
  createReviewDecision,
  fetchOutreach,
  fetchReviewDecisions,
  fetchReviewQueue,
} from '../api/endpoints';
import type {
  ActionOption,
  OutreachAction,
  ReviewDecision,
  ReviewDecisionInput,
} from '../api/types';
import { EmptyState, ErrorMessage } from '../components/Messages';
import { PageHeader } from '../components/PageHeader';
import { PriorityBadge } from '../components/PriorityBadge';
import { truncate } from '../util/labels';

function Bucket({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <section className="bucket">
      <h2>
        {title} <span className="count">{count}</span>
      </h2>
      {children}
    </section>
  );
}

function LeadTitle({ action }: { action: OutreachAction }) {
  return (
    <div className="card-title">
      {action.lead.agency_name} — {action.lead.contact_name}
      <PriorityBadge priority={action.priority} />
    </div>
  );
}

interface ReviewCardProps {
  action: OutreachAction;
  options: ActionOption[];
  reviewer: string;
  onSubmit: (decision: ReviewDecisionInput) => Promise<void>;
}

/** One needs_human item: pick a pre-defined action, or propose a new one. */
function ReviewCard({ action, options, reviewer, onSubmit }: ReviewCardProps) {
  const [selected, setSelected] = useState(options[0]?.value ?? '');
  const [proposing, setProposing] = useState(false);
  const [proposal, setProposal] = useState({ name: '', what: '', when: '' });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(decision: ReviewDecisionInput) {
    setError(null);
    setSaving(true);
    try {
      await onSubmit(decision);
    } catch (err) {
      setError(`Could not save decision: ${errorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card">
      <LeadTitle action={action} />
      <div className="card-detail">
        <span className="label">Why:</span> {action.reason}
      </div>

      <div className="review-controls">
        <select
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
          aria-label="Action type"
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="action"
          disabled={saving || !selected}
          onClick={() =>
            submit({
              outreach_action: action.id,
              kind: 'select_existing',
              selected_action_type: selected,
              reviewer,
            })
          }
        >
          Pick
        </button>
        <button
          type="button"
          className="secondary action"
          onClick={() => setProposing((open) => !open)}
        >
          Propose new
        </button>
      </div>

      {proposing && (
        <div className="propose-form">
          <input
            type="text"
            placeholder="Name (short label for the new action)"
            value={proposal.name}
            onChange={(event) =>
              setProposal({ ...proposal, name: event.target.value })
            }
          />
          <input
            type="text"
            placeholder="What should happen"
            value={proposal.what}
            onChange={(event) =>
              setProposal({ ...proposal, what: event.target.value })
            }
          />
          <input
            type="text"
            placeholder="When it should trigger"
            value={proposal.when}
            onChange={(event) =>
              setProposal({ ...proposal, when: event.target.value })
            }
          />
          <button
            type="button"
            className="action"
            disabled={saving}
            onClick={() =>
              submit({
                outreach_action: action.id,
                kind: 'propose_new',
                proposed_name: proposal.name,
                proposed_what: proposal.what,
                proposed_when: proposal.when,
                reviewer,
              })
            }
          >
            Submit proposal
          </button>
        </div>
      )}

      {error && <div className="card-error">{error}</div>}
    </div>
  );
}

export function DashboardPage() {
  const [outreach, setOutreach] = useState<OutreachAction[]>([]);
  const [queue, setQueue] = useState<OutreachAction[]>([]);
  const [options, setOptions] = useState<ActionOption[]>([]);
  const [decisions, setDecisions] = useState<ReviewDecision[]>([]);
  const [reviewer, setReviewer] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  /** Repaint the live sections (queue + logs) from the server. */
  const refresh = useCallback(async () => {
    const [reviewQueue, reviewDecisions] = await Promise.all([
      fetchReviewQueue(),
      fetchReviewDecisions(),
    ]);
    setQueue(reviewQueue.items);
    setOptions(reviewQueue.action_options);
    setDecisions(reviewDecisions);
  }, []);

  useEffect(() => {
    let active = true;
    Promise.all([fetchOutreach(), fetchReviewQueue(), fetchReviewDecisions()])
      .then(([actions, reviewQueue, reviewDecisions]) => {
        if (!active) return;
        setOutreach(actions);
        setQueue(reviewQueue.items);
        setOptions(reviewQueue.action_options);
        setDecisions(reviewDecisions);
      })
      .catch((err: unknown) => {
        if (active) setError(`Failed to load dashboard: ${errorMessage(err)}`);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  async function handleDecision(decision: ReviewDecisionInput) {
    await createReviewDecision(decision);
    // Drop the card immediately for instant feedback, then let the server be
    // the source of truth for the queue and both logs.
    setQueue((items) => items.filter((item) => item.id !== decision.outreach_action));
    try {
      await refresh();
    } catch (err) {
      setError(`Failed to refresh review data: ${errorMessage(err)}`);
    }
  }

  const taken = outreach.filter((a) => !a.needs_human && a.suggested_copy?.trim());
  const followUp = outreach.filter((a) => !a.needs_human && a.further_action?.trim());
  const resolved = decisions.filter((d) => d.status === 'resolved');
  const pending = decisions.filter((d) => d.status === 'pending_engineering');

  const labelFor = (value: string | null) =>
    options.find((option) => option.value === value)?.label ?? value ?? 'unknown';

  const loadingState = <EmptyState>Loading…</EmptyState>;

  return (
    <>
      <PageHeader
        current="/next-actions/"
        title="BD Dashboard"
        subtitle="Outreach actions taken, follow-ups, and the manual review queue — one place for the team"
      />

      <div className="container narrow">
        {error && <ErrorMessage>{error}</ErrorMessage>}

        <div className="reviewer-bar">
          <label htmlFor="reviewer">Reviewer</label>
          <input
            id="reviewer"
            type="text"
            placeholder="Your name (used when you pick or propose an action)"
            value={reviewer}
            onChange={(event) => setReviewer(event.target.value)}
          />
        </div>

        <Bucket title="Outreach drafted" count={taken.length}>
          {loading ? (
            loadingState
          ) : taken.length === 0 ? (
            <EmptyState>No outreach has been drafted yet.</EmptyState>
          ) : (
            taken.map((action) => (
              <div className="card" key={action.id}>
                <LeadTitle action={action} />
                <div className="card-detail">
                  <span className="label">Action:</span> {action.action_type}
                </div>
                <div className="card-detail snippet">
                  {truncate(action.suggested_copy ?? '', 160)}
                </div>
              </div>
            ))
          )}
        </Bucket>

        <Bucket title="Needs follow-up" count={followUp.length}>
          {loading ? (
            loadingState
          ) : followUp.length === 0 ? (
            <EmptyState>No outstanding follow-ups. 🎉</EmptyState>
          ) : (
            followUp.map((action) => (
              <div className="card" key={action.id}>
                <LeadTitle action={action} />
                <div className="card-detail">
                  <span className="label">Follow-up:</span> {action.further_action}
                </div>
              </div>
            ))
          )}
        </Bucket>

        <Bucket title="Needs manual review" count={queue.length}>
          {loading ? (
            loadingState
          ) : queue.length === 0 ? (
            <EmptyState>Nothing needs manual review right now. 🎉</EmptyState>
          ) : (
            queue.map((action) => (
              <ReviewCard
                key={action.id}
                action={action}
                options={options}
                reviewer={reviewer.trim()}
                onSubmit={handleDecision}
              />
            ))
          )}
        </Bucket>

        <Bucket title="Resolved log" count={resolved.length}>
          {loading ? (
            loadingState
          ) : resolved.length === 0 ? (
            <EmptyState>No resolved decisions yet.</EmptyState>
          ) : (
            resolved.map((decision) => (
              <div className="log-item" key={decision.id}>
                <div>
                  <span className="label">
                    {labelFor(decision.selected_action_type)}
                  </span>{' '}
                  — action #{decision.outreach_action}
                </div>
                <div className="who">
                  Resolved by {decision.reviewer || 'unknown'}
                </div>
              </div>
            ))
          )}
        </Bucket>

        <Bucket title="Pending engineering" count={pending.length}>
          {loading ? (
            loadingState
          ) : pending.length === 0 ? (
            <EmptyState>No proposals waiting on engineering.</EmptyState>
          ) : (
            pending.map((decision) => (
              <div className="log-item" key={decision.id}>
                <div>
                  <span className="label">{decision.proposed_name}</span> — action #
                  {decision.outreach_action}
                </div>
                <div className="card-detail">
                  <span className="label">What:</span> {decision.proposed_what}
                </div>
                <div className="card-detail">
                  <span className="label">When:</span> {decision.proposed_when}
                </div>
                <div className="who">
                  Proposed by {decision.reviewer || 'unknown'}
                </div>
              </div>
            ))
          )}
        </Bucket>
      </div>
    </>
  );
}
