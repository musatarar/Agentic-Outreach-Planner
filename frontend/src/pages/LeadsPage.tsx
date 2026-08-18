import { useEffect, useState } from 'react';
import { errorMessage } from '../api/client';
import { fetchLeads, fetchQueue } from '../api/endpoints';
import type { LeadRecord } from '../api/types';
import { EmptyState, ErrorMessage } from '../components/Messages';
import { PageHeader } from '../components/PageHeader';
import { LeadsTable } from '../components/leads/LeadsTable';
import { DEFAULT_SORT, queuedLeadIds, sortLeads } from '../components/leads/leadTable';
import type { SortKey, SortState } from '../components/leads/leadTable';
import '../components/leads/leads.css';

/**
 * The book of leads — where signing in lands you.
 *
 * Two requests, with deliberately different failure handling. The leads are the
 * page: without them there is nothing to render, so a failure there is fatal
 * and shows an error. The queue is only used to flag which leads already have
 * an open recommendation; losing it costs a badge, not the page, so it degrades
 * to a visible warning rather than an empty screen. It is *visible* rather than
 * a console line because the flags are what stop the next PR's "select all"
 * from spending a provider call on leads that can only answer 409.
 */
export function LeadsPage() {
  const [leads, setLeads] = useState<LeadRecord[]>([]);
  const [queued, setQueued] = useState<Set<string>>(new Set());
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [error, setError] = useState<string | null>(null);
  const [queueWarning, setQueueWarning] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

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

    fetchQueue()
      .then((response) => {
        if (active) setQueued(queuedLeadIds(response.items));
      })
      .catch(() => {
        if (active) {
          setQueueWarning(
            'Could not read the triage queue, so leads already awaiting review are not flagged below.',
          );
        }
      });

    return () => {
      active = false;
    };
  }, []);

  /** Clicking the sorted column reverses it; any other column starts ascending. */
  function handleSort(key: SortKey) {
    setSort((current) =>
      current.key === key
        ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' }
        : { key, direction: 'asc' },
    );
  }

  const ordered = sortLeads(leads, sort.key, sort.direction);

  return (
    <>
      <PageHeader
        current="/leads/"
        title="Leads"
        subtitle="The whole book, stalest contact first — start here to decide who needs outreach"
      />

      <div className="container">
        {error && <ErrorMessage>{error}</ErrorMessage>}
        {queueWarning && <div className="leads-warning">{queueWarning}</div>}

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
              {ordered.length} leads · {queued.size} already in the review queue
            </p>
            <LeadsTable leads={ordered} sort={sort} onSort={handleSort} queued={queued} />
          </>
        )}
      </div>
    </>
  );
}
