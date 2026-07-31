import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { errorMessage } from '../api/client';
import { fetchLLMConfig, fetchOutreach, runOutreachPlan } from '../api/endpoints';
import type { LLMConfig, OutreachAction } from '../api/types';
import { CopyButton } from '../components/CopyButton';
import { ErrorMessage } from '../components/Messages';
import { PageHeader } from '../components/PageHeader';
import { PriorityBadge } from '../components/PriorityBadge';
import { formatActionType, formatTimestamp } from '../util/labels';

function ActionCard({ action }: { action: OutreachAction }) {
  return (
    <div className={`card p${action.priority}`}>
      <div className="card-header">
        <div className="who">{action.lead.agency_name}</div>
        <div className="contact-info">
          {action.lead.contact_name}
          <br />
          <a href={`mailto:${action.lead.contact_email}`}>
            {action.lead.contact_email}
          </a>
        </div>
        <div className="meta">
          <PriorityBadge priority={action.priority} variant="priority-badge" />
          <span className="action-label">
            {formatActionType(action.action_type)}
          </span>
        </div>
      </div>

      <div className="reason">
        <span className="reason-label">Why:</span>
        {action.reason}
      </div>

      {action.suggested_copy && (
        <div className="suggested-copy-section">
          <span className="suggested-copy-label">Suggested Copy:</span>
          <div className="suggested-copy">{action.suggested_copy}</div>
          <CopyButton text={action.suggested_copy} />
        </div>
      )}

      {action.needs_human && (
        <div className="needs-human">
          <span className="needs-human-title">Needs Human Review</span>
          <div className="needs-human-action">
            {action.further_action || 'Please review this action manually.'}
          </div>
        </div>
      )}

      <div className="timestamp">Created: {formatTimestamp(action.created_at)}</div>
    </div>
  );
}

export function PlannerPage() {
  const [actions, setActions] = useState<OutreachAction[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [llmConfig, setLlmConfig] = useState<LLMConfig | null>(null);

  // Show whatever the last run produced. A failure here is non-fatal — the
  // page still offers the Run button, matching the old template's behaviour.
  useEffect(() => {
    let active = true;
    fetchOutreach()
      .then((latest) => {
        if (active) setActions(latest);
      })
      .catch((err: unknown) => {
        console.error('Error loading latest plan:', err);
      });
    return () => {
      active = false;
    };
  }, []);

  // Non-fatal: if this fails, Run stays enabled and just relies on the
  // backend to error out per-lead as before.
  useEffect(() => {
    let active = true;
    fetchLLMConfig()
      .then((config) => {
        if (active) setLlmConfig(config);
      })
      .catch((err: unknown) => {
        console.error('Error loading LLM config:', err);
      });
    return () => {
      active = false;
    };
  }, []);

  const noKeyConfigured = llmConfig?.key_source === 'none';

  async function handleRun() {
    setError(null);
    setActions([]);
    setRunning(true);
    try {
      setActions(await runOutreachPlan());
    } catch (err) {
      setError(errorMessage(err));
      console.error('Error running outreach plan:', err);
    } finally {
      setRunning(false);
    }
  }

  return (
    <>
      <PageHeader
        current="/"
        title="Outreach Planner"
        subtitle="Identify which leads need outreach today and generate suggested messaging"
      >
        <div className="controls">
          <button type="button" onClick={handleRun} disabled={running || noKeyConfigured}>
            Run Outreach Plan
          </button>
          {llmConfig && !noKeyConfigured && (
            <span className="status llm-status">
              using {llmConfig.provider}/{llmConfig.model}
            </span>
          )}
          {noKeyConfigured && (
            <span className="status llm-status-warning">
              No API key configured — <Link to="/settings/">set one up in Settings</Link>
            </span>
          )}
          {running && (
            <span className="status">
              <span className="loading-spinner" />{' '}
              <span>Running outreach plan (this may take 15-30 seconds)...</span>
            </span>
          )}
        </div>
      </PageHeader>

      <div className="container">
        {error && <ErrorMessage>Error: {error}</ErrorMessage>}
        {actions.length > 0 ? (
          <div className="cards">
            {actions.map((action) => (
              <ActionCard key={action.id} action={action} />
            ))}
          </div>
        ) : (
          !running &&
          !error && (
            <div className="no-results">
              <p>
                No outreach actions yet. Click "Run Outreach Plan" to generate
                actions.
              </p>
            </div>
          )
        )}
      </div>
    </>
  );
}
