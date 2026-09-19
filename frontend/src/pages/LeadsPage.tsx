import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError, errorMessage } from '../api/client';
import { fetchAllProposals, fetchLeads, fetchOutreach, generateFromProposal } from '../api/endpoints';
import type { LeadRecord, ProposedAction } from '../api/types';
import { EmptyState, ErrorMessage } from '../components/Messages';
import { PageHeader } from '../components/PageHeader';
import { Button } from '../components/ui';
import { LeadsTable } from '../components/leads/LeadsTable';
import { DEFAULT_SORT, openLeadIds, proposalsByLead, sortLeads } from '../components/leads/leadTable';
import type { SortKey, SortState } from '../components/leads/leadTable';
import { withDraft } from '../components/leads/proposals';
import '../components/leads/leads.css';

/**
 * The book of leads — where signing in lands you, and where the engine's
 * choices are read and drafted.
 *
 * Three requests, with deliberately different failure handling. The leads are
 * the page: without them there is nothing to render, so a failure there is
 * fatal and shows an error. The other two degrade to a visible warning rather
 * than an empty screen — losing the proposals costs the column, losing the
 * inbox costs a badge. Both warnings are *visible* rather than console lines
 * because a blank column otherwise reads as "the engine chose nothing".
 *
 * Nothing here plans the whole book. The engine decides on its own cron; a
 * click only turns one decision it already made into copy.
 */
export function LeadsPage() {
  const navigate = useNavigate();
  const [leads, setLeads] = useState<LeadRecord[]>([]);
  const [proposals, setProposals] = useState<ProposedAction[]>([]);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [error, setError] = useState<string | null>(null);
  const [inboxWarning, setInboxWarning] = useState<string | null>(null);
  const [proposalsWarning, setProposalsWarning] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** The one lead whose decision is expanded, if any. */
  const [expanded, setExpanded] = useState<string | null>(null);
  /** The proposal currently being drafted, so only its button spins. */
  const [generating, setGenerating] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const loadOpenItems = useCallback(async () => {
    try {
      setOpen(openLeadIds((await fetchOutreach()).results));
      setInboxWarning(null);
    } catch {
      setInboxWarning(
        'Could not read the review inbox, so leads already awaiting review are not flagged below.',
      );
    }
  }, []);

  const loadProposals = useCallback(async () => {
    try {
      setProposals(await fetchAllProposals());
      setProposalsWarning(null);
    } catch {
      setProposalsWarning(
        'Could not read what the engine chose, so the proposed action column is empty below.',
      );
    }
  }, []);

  useEffect(() => {
    let active = true;

    fetchLeads()
      .then((records) => {
        if (active) setLeads(records);
      })
      .catch((err: unknown) => {
        if (active) setError(`Failed to load leads: ${errorMessage(err)}`);
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    void loadProposals();
    void loadOpenItems();

    return () => {
      active = false;
    };
  }, [loadOpenItems, loadProposals]);

  /** Clicking the sorted column reverses it; any other column starts ascending. */
  function handleSort(key: SortKey) {
    setSort((current) =>
      current.key === key
        ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' }
        : { key, direction: 'asc' },
    );
  }

  /** One open row at a time; clicking the open one closes it. */
  function handleToggle(leadId: string) {
    setExpanded((current) => (current === leadId ? null : leadId));
  }

  /**
   * Draft the copy for one decision. A 409 means the server declined — already
   * drafted, or the recommendation was dismissed — so the proposals are re-read
   * rather than guessed at, and the row settles into whichever state it is
   * really in.
   */
  async function handleGenerate(proposal: ProposedAction) {
    setNotice(null);
    setError(null);
    setGenerating(proposal.id);
    try {
      const draft = await generateFromProposal(proposal.id);
      setProposals((current) => withDraft(current, proposal.id, draft.id));
      await loadOpenItems();
      setNotice(
        `Drafted ${proposal.action.label} for ${proposal.lead.agency_name}. Review it in the inbox.`,
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice(err.message);
        await loadProposals();
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setGenerating(null);
    }
  }

  const ordered = sortLeads(leads, sort.key, sort.direction);
  const byLead = useMemo(() => proposalsByLead(proposals), [proposals]);

  return (
    <>
      <PageHeader
        current="/leads/"
        title="Leads"
        subtitle="The whole book, stalest contact first — open a lead to see what the engine chose and draft the email"
      >
        <div className="controls">
          <Button variant="ghost" onClick={() => navigate('/inbox')}>
            Go to inbox
          </Button>
        </div>
      </PageHeader>

      <div className="container">
        {error && <ErrorMessage>{error}</ErrorMessage>}
        {proposalsWarning && <div className="leads-warning">{proposalsWarning}</div>}
        {inboxWarning && <div className="leads-warning">{inboxWarning}</div>}
        {notice && <div className="leads-notice">{notice}</div>}

        {loading ? (
          <EmptyState>Loading…</EmptyState>
        ) : ordered.length === 0 ? (
          <EmptyState>
            No leads yet. Run <code>python scripts/populate_demo_data.py</code> to load the
            demo book.
          </EmptyState>
        ) : (
          <>
            <p className="leads-count">
              {ordered.length} leads · {byLead.size} with a proposed action · {open.size}{' '}
              awaiting review
            </p>
            <LeadsTable
              leads={ordered}
              sort={sort}
              onSort={handleSort}
              open={open}
              proposals={byLead}
              expanded={expanded}
              onToggle={handleToggle}
              generating={generating}
              onGenerate={(proposal) => void handleGenerate(proposal)}
            />
          </>
        )}
      </div>
    </>
  );
}
