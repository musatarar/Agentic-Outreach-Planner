import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError, errorMessage } from '../api/client';
import { composeForLead, fetchLeads, fetchOutreach, runOutreachPlan } from '../api/endpoints';
import type { LeadRecord } from '../api/types';
import { EmptyState, ErrorMessage } from '../components/Messages';
import { PageHeader } from '../components/PageHeader';
import { Button } from '../components/ui';
import { LeadsTable } from '../components/leads/LeadsTable';
import { DEFAULT_SORT, openLeadIds, sortLeads } from '../components/leads/leadTable';
import type { SortKey, SortState } from '../components/leads/leadTable';
import '../components/leads/leads.css';

/**
 * The book of leads — where signing in lands you, and where drafts are
 * generated from.
 *
 * Two requests, with deliberately different failure handling. The leads are the
 * page: without them there is nothing to render, so a failure there is fatal
 * and shows an error. The inbox is only used to flag which leads already have
 * an open recommendation; losing it costs a badge, not the page, so it degrades
 * to a visible warning rather than an empty screen. It is *visible* rather than
 * a console line because those flags are what stop a Generate click from
 * spending a provider call on a lead that can only answer 409.
 */
export function LeadsPage() {
  const navigate = useNavigate();
  const [leads, setLeads] = useState<LeadRecord[]>([]);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [error, setError] = useState<string | null>(null);
  const [inboxWarning, setInboxWarning] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  /** The lead id currently being composed for, so only its button spins. */
  const [composing, setComposing] = useState<string | null>(null);
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

    void loadOpenItems();

    return () => {
      active = false;
    };
  }, [loadOpenItems]);

  /** Clicking the sorted column reverses it; any other column starts ascending. */
  function handleSort(key: SortKey) {
    setSort((current) =>
      current.key === key
        ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' }
        : { key, direction: 'asc' },
    );
  }

  /** Plan the whole book; the drafts land in the inbox. */
  async function handleRunAll() {
    setNotice(null);
    setError(null);
    setRunning(true);
    try {
      const planned = await runOutreachPlan();
      await loadOpenItems();
      setNotice(
        planned.length === 0
          ? 'Nothing new to generate — every lead already has an open recommendation or a dismissal.'
          : `Generated ${planned.length} draft${planned.length === 1 ? '' : 's'}. Review them in the inbox.`,
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setRunning(false);
    }
  }

  /** Plan one client; 409 means the planner declined, not a failure. */
  async function handleCompose(leadId: string) {
    setNotice(null);
    setError(null);
    setComposing(leadId);
    try {
      await composeForLead(leadId);
      await loadOpenItems();
      setNotice(`Generated a draft for ${leadId}. Review it in the inbox.`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice(
          `${leadId} already has an open recommendation, or was dismissed — nothing to generate.`,
        );
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setComposing(null);
    }
  }

  const ordered = sortLeads(leads, sort.key, sort.direction);

  return (
    <>
      <PageHeader
        current="/leads/"
        title="Leads"
        subtitle="The whole book, stalest contact first — start here to decide who needs outreach"
      >
        <div className="controls">
          <Button variant="primary" loading={running} onClick={() => void handleRunAll()}>
            Generate all
          </Button>
          <Button variant="ghost" onClick={() => navigate('/inbox')}>
            Go to inbox
          </Button>
          {running && (
            <span className="status">Generating drafts (this may take 15-30 seconds)…</span>
          )}
        </div>
      </PageHeader>

      <div className="container">
        {error && <ErrorMessage>{error}</ErrorMessage>}
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
              {ordered.length} leads · {open.size} awaiting review
            </p>
            <LeadsTable
              leads={ordered}
              sort={sort}
              onSort={handleSort}
              open={open}
              composing={composing}
              onCompose={(leadId) => void handleCompose(leadId)}
            />
          </>
        )}
      </div>
    </>
  );
}
