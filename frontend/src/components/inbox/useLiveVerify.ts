import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { verifyQueueCopy } from '../../api/endpoints';
import type { VerificationReport } from '../../api/types';

/** the debounce; the endpoint is throttled at 120/min. */
const DEBOUNCE_MS = 250;

export interface LiveVerifyResult {
  /** The report to render: the live one while editing, else the committed one. */
  report: VerificationReport | null;
  verifying: boolean;
  error: string | null;
  /** True when `report` came from a dry-run rather than from the queue item. */
  isLive: boolean;
}

/**
 * Re-verify edited copy while the user types. `POST /verify/` is a dry run —
 * only `/edit/` persists. The response echoes the exact copy it verified; if
 * that no longer matches the textarea it is an out-of-order debounced reply
 * and is discarded, never rendered against newer text.
 */
export function useLiveVerify(
  // Nullable so the hook call stays unconditional while the queue loads/drains.
  itemId: number | null,
  committedReport: VerificationReport | null,
  draft: string,
  active: boolean,
): LiveVerifyResult {
  const [liveReport, setLiveReport] = useState<VerificationReport | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The textarea's current value, readable from inside stale async closures.
  const draftRef = useRef(draft);
  draftRef.current = draft;

  // Monotonic request token: a superseded slow reply must not win.
  const latestRequest = useRef(0);

  // A new lead, or a fresh committed report from /edit/, resets the overlay.
  useEffect(() => {
    setLiveReport(null);
    setError(null);
  }, [itemId, committedReport]);

  useEffect(() => {
    if (!active || itemId === null || committedReport === null) return;
    if (draft === committedReport.copy) {
      // Back to what the server last verified — nothing to ask.
      setLiveReport(null);
      setError(null);
      return;
    }

    const timer = window.setTimeout(() => {
      const token = (latestRequest.current += 1);
      setVerifying(true);
      verifyQueueCopy(itemId, { copy: draft })
        .then((response) => {
          if (token !== latestRequest.current) return;
          if (response.copy !== draftRef.current) return; // stale
          setLiveReport(response);
          setError(null);
        })
        .catch((err: unknown) => {
          if (token !== latestRequest.current) return;
          setError(errorMessage(err));
        })
        .finally(() => {
          if (token === latestRequest.current) setVerifying(false);
        });
    }, DEBOUNCE_MS);

    return () => window.clearTimeout(timer);
  }, [active, draft, committedReport, itemId]);

  return {
    report: liveReport ?? committedReport,
    verifying,
    error,
    isLive: liveReport !== null,
  };
}
