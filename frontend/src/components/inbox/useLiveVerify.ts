import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { verifyQueueCopy } from '../../api/endpoints';
import type { VerificationReport } from '../../api/types';

/** CONTRACT §5.2 pins the debounce; the endpoint is throttled at 120/min. */
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
 * Re-verify edited copy while the user types, so the underlines answer back.
 *
 * Editing a number to something the record does not support should turn its
 * underline red immediately — that feedback is the whole reason inline editing
 * is worth building rather than shipping a plain textarea.
 *
 * Two edges from CONTRACT §9.2 are load-bearing:
 *
 *  - `POST /verify/` is a dry run; nothing is persisted. Only `/edit/` commits.
 *  - The response **echoes the exact copy it verified**. If that no longer
 *    matches the textarea, the reply is an out-of-order debounced request and
 *    is discarded rather than rendered. Rendering it would draw spans computed
 *    against one string over the text of another, which is precisely the class
 *    of bug that puts a green underline under a number nobody checked.
 */
export function useLiveVerify(
  // Nullable so the caller can keep the hook call unconditional while the queue
  // is still loading or has drained.
  itemId: number | null,
  committedReport: VerificationReport | null,
  draft: string,
  active: boolean,
): LiveVerifyResult {
  const [liveReport, setLiveReport] = useState<VerificationReport | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // What the textarea holds *right now*, readable from inside an async callback
  // that closed over an older value.
  const draftRef = useRef(draft);
  draftRef.current = draft;

  // Monotonic request token: a slow reply that has been superseded by a newer
  // one must not win, even if it happens to still match the draft.
  const latestRequest = useRef(0);

  // A new lead, or a fresh committed report from /edit/, resets the overlay.
  useEffect(() => {
    setLiveReport(null);
    setError(null);
  }, [itemId, committedReport]);

  useEffect(() => {
    if (!active || itemId === null || committedReport === null) return;
    if (draft === committedReport.copy) {
      // Back to exactly what the server last verified — the committed report is
      // already correct, so there is nothing to ask.
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
          if (response.copy !== draftRef.current) return; // stale (§9.2)
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
