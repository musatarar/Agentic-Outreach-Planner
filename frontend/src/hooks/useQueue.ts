import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '../api/client';
import { fetchOutreach } from '../api/endpoints';
import type { ReviewItem } from '../api/types';

/**
 * The review inbox, prefetched once. `GET /api/outreach/` returns complete
 * items, so deciding one is a state change rather than a refetch — a decided
 * item stays in place with its new status, because every decision can be
 * reopened.
 */

export interface UseQueueResult {
  loading: boolean;
  error: string | null;
  /** The page of items, server order (priority ASC, lead ASC). */
  items: ReviewItem[];
  /** Total items the server holds, which may exceed this page. */
  total: number;
  /** How many of the loaded items are still awaiting a decision. */
  pending: number;
  /** Swap one item in place after a mutation returns a fresh item. */
  replace: (item: ReviewItem) => void;
  reload: () => Promise<void>;
}

export function useQueue(): UseQueueResult {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [total, setTotal] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchOutreach();
      setItems(response.results);
      setTotal(response.count);
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

  const replace = useCallback((updated: ReviewItem) => {
    setItems((current) =>
      current.map((entry) => (entry.id === updated.id ? updated : entry)),
    );
  }, []);

  const pending = useMemo(
    () => items.filter((item) => item.status === 'pending').length,
    [items],
  );

  return { loading, error, items, total, pending, replace, reload: load };
}
