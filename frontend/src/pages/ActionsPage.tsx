import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError, errorMessage } from '../api/client';
import { fetchProposals, generateFromProposal } from '../api/endpoints';
import type { ProposedAction } from '../api/types';
import { EmptyState, ErrorMessage } from '../components/Messages';
import { PageHeader } from '../components/PageHeader';
import { Badge, Button, Card } from '../components/ui';
import { canGenerate, urgencyTone, withDraft } from '../components/actions/proposals';
import { formatTimestamp } from '../util/labels';
import '../components/actions/actions.css';

/**
 * What the actions engine already decided, one card per lead.
 *
 * The engine runs on its own cron and stops at a chosen action; this page is
 * where a human turns one of those choices into copy. Generating here drafts
 * exactly one action for one lead — it is not the whole-book planner run the
 * leads page offers, and the two write into the same inbox.
 */
export function ActionsPage() {
  const navigate = useNavigate();
  const [proposals, setProposals] = useState<ProposedAction[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** The proposal being drafted, so only its button spins. */
  const [generating, setGenerating] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setProposals((await fetchProposals()).results);
  }, []);

  useEffect(() => {
    let active = true;
    load()
      .catch((err: unknown) => {
        if (active) setError(`Failed to load proposed actions: ${errorMessage(err)}`);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [load]);

  /**
   * Draft one proposal. A 409 means the server declined — already drafted, or
   * the recommendation was dismissed — so the list is re-read rather than
   * guessed at, and the row settles into whichever state it is really in.
   */
  async function handleGenerate(proposal: ProposedAction) {
    setNotice(null);
    setError(null);
    setGenerating(proposal.id);
    try {
      const draft = await generateFromProposal(proposal.id);
      setProposals((current) => withDraft(current, proposal.id, draft.id));
      setNotice(`Drafted ${proposal.action.label} for ${proposal.lead.agency_name}.`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice(err.message);
        await load().catch(() => undefined);
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setGenerating(null);
    }
  }

  return (
    <>
      <PageHeader
        current="/actions"
        title="Proposed actions"
        subtitle="What your rules chose, newest first — generate copy for one, then review it in the inbox"
      >
        <div className="controls">
          <Button variant="ghost" onClick={() => navigate('/inbox')}>
            Go to inbox
          </Button>
        </div>
      </PageHeader>

      <div className="container">
        {error && <ErrorMessage>{error}</ErrorMessage>}
        {notice && <div className="proposals-notice">{notice}</div>}

        {loading ? (
          <EmptyState>Loading…</EmptyState>
        ) : proposals.length === 0 ? (
          <EmptyState>
            Nothing proposed yet. The engine queues your leads and runs your rules on its own
            schedule; leads it cannot make a case for stay here unlisted.
          </EmptyState>
        ) : (
          <ul className="proposals">
            {proposals.map((proposal) => (
              <li key={proposal.id}>
                <Card as="article" elevation="raised">
                  <div className="proposal__head">
                    <div>
                      <h2 className="proposal__agency">{proposal.lead.agency_name}</h2>
                      <p className="proposal__contact">
                        {proposal.lead.contact_name}
                        <span className="proposal__id">{proposal.lead.id}</span>
                      </p>
                    </div>
                    <Badge tone={urgencyTone(proposal.action.urgency)}>
                      {proposal.action.label}
                    </Badge>
                  </div>

                  <ul className="proposal__reasons">
                    {proposal.reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>

                  <div className="proposal__foot">
                    <span className="proposal__decided">
                      Weight {proposal.weight ?? '—'} · decided{' '}
                      {proposal.decided_at ? formatTimestamp(proposal.decided_at) : 'unknown'}
                    </span>
                    {canGenerate(proposal) ? (
                      <Button
                        size="sm"
                        loading={generating === proposal.id}
                        disabled={generating !== null}
                        onClick={() => void handleGenerate(proposal)}
                      >
                        Generate
                      </Button>
                    ) : (
                      <Link className="proposal__draft" to="/inbox">
                        Drafted — review it in the inbox
                      </Link>
                    )}
                  </div>
                </Card>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
