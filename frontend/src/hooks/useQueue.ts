import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '../api/client';
import { fetchDone, fetchQueue } from '../api/endpoints';
import type { DoneSummary, QueueCounts, QueueItem } from '../api/types';

/**
 * The triage queue, prefetched once. `GET /api/queue/` returns complete
 * QueueItems, so navigation and `settle` perform zero network requests.
 */

export type QueueOutcome = 'approved' | 'snoozed' | 'dismissed';

/** One item the user has resolved this session, kept for the empty state. */
export interface SettledItem {
  item: QueueItem;
  outcome: QueueOutcome;
}

export interface UseQueueResult {
  loading: boolean;
  error: string | null;
  /** The server's "today" (settings.TRIAGE_TIMEZONE); never local new Date(). */
  date: string;
  timezone: string;
  counts: QueueCounts;
  /** The working set: pending items, server order (priority ASC, lead ASC). */
  items: QueueItem[];
  index: number;
  current: QueueItem | null;
  settled: SettledItem[];
  /** True once a successful load leaves nothing left to triage. */
  cleared: boolean;
  /** Server-computed end-of-day figures, fetched once when the queue clears. */
  doneSummary: DoneSummary | null;
  doneSummaryError: string | null;
  select: (index: number) => void;
  next: () => void;
  previous: () => void;
  /** Swap one item in place after a mutation returns a fresh QueueItem. */
  replace: (item: QueueItem) => void;
  /** Drop a resolved item from the working set and slide the next one in. */
  settle: (item: QueueItem, outcome: QueueOutcome) => void;
  reload: () => Promise<void>;
}

const EMPTY_COUNTS: QueueCounts = {
  total_today: 0,
  done_today: 0,
  remaining: 0,
  approved_today: 0,
  snoozed_today: 0,
  dismissed_today: 0,
};

export function useQueue(): UseQueueResult {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [date, setDate] = useState('');
  const [timezone, setTimezone] = useState('');
  // Server-reported baseline, held apart so a reload resets to server truth.
  const [baseCounts, setBaseCounts] = useState<QueueCounts>(EMPTY_COUNTS);
  const [items, setItems] = useState<QueueItem[]>([]);
  const [index, setIndex] = useState(0);
  const [settled, setSettled] = useState<SettledItem[]>([]);
  const [doneSummary, setDoneSummary] = useState<DoneSummary | null>(null);
  const [doneSummaryError, setDoneSummaryError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchQueue();
      setDate(response.date);
      setTimezone(response.timezone);
      setBaseCounts(response.counts);
      setItems(response.items);
      setIndex(0);
      setSettled([]);
      setDoneSummary(null);
      setDoneSummaryError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  // Guard against StrictMode's dev double-invoke firing two prefetches.
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void load();
  }, [load]);

  const cleared = !loading && error === null && items.length === 0;

  // /api/queue/done/ is the authority on pipeline value and elapsed time;
  // deriving them locally would disagree with /done after a tab reload.
  useEffect(() => {
    if (!cleared) return;
    let live = true;
    fetchDone()
      .then((response) => {
        if (live) setDoneSummary(response.summary);
      })
      .catch((err: unknown) => {
        if (live) setDoneSummaryError(errorMessage(err));
      });
    return () => {
      live = false;
    };
  }, [cleared]);

  // Mutations return a QueueItem, not fresh counts, so adjust the server
  // baseline by what this session has resolved.
  const counts = useMemo<QueueCounts>(() => {
    const by = (outcome: QueueOutcome) =>
      settled.filter((entry) => entry.outcome === outcome).length;
    return {
      total_today: baseCounts.total_today,
      done_today: baseCounts.done_today + settled.length,
      remaining: items.length,
      approved_today: baseCounts.approved_today + by('approved'),
      snoozed_today: baseCounts.snoozed_today + by('snoozed'),
      dismissed_today: baseCounts.dismissed_today + by('dismissed'),
    };
  }, [baseCounts, items.length, settled]);

  const lastIndex = Math.max(0, items.length - 1);

  const select = useCallback(
    (target: number) => {
      setIndex(Math.min(Math.max(0, target), lastIndex));
    },
    [lastIndex],
  );

  // Clamped, not wrapping: holding J rests on the last lead.
  const next = useCallback(() => {
    setIndex((current) => Math.min(current + 1, lastIndex));
  }, [lastIndex]);

  const previous = useCallback(() => {
    setIndex((current) => Math.max(current - 1, 0));
  }, []);

  const replace = useCallback((updated: QueueItem) => {
    setItems((current) =>
      current.map((entry) => (entry.id === updated.id ? updated : entry)),
    );
  }, []);

  const settle = useCallback((item: QueueItem, outcome: QueueOutcome) => {
    setSettled((current) => [...current, { item, outcome }]);
    setItems((current) => {
      const remaining = current.filter((entry) => entry.id !== item.id);
      // Hold position: the next lead slides into the vacated slot.
      setIndex((currentIndex) =>
        Math.min(currentIndex, Math.max(0, remaining.length - 1)),
      );
      return remaining;
    });
  }, []);

  // Clamp defensively during the render that follows a settle.
  const safeIndex = items.length === 0 ? 0 : Math.min(index, items.length - 1);
  const current = items[safeIndex] ?? null;

  return {
    loading,
    error,
    date,
    timezone,
    counts,
    items,
    index: safeIndex,
    current,
    settled,
    cleared,
    doneSummary,
    doneSummaryError,
    select,
    next,
    previous,
    replace,
    settle,
    reload: load,
  };
}
