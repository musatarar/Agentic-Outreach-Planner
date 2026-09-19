import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { verifyCopy } from '../../api/endpoints';
import type { CopyPair, VerificationReport } from '../../api/types';

/** the debounce; the endpoint is throttled at 120/min. */
const DEBOUNCE_MS = 250;

export function samePair(a: CopyPair, b: CopyPair): boolean {
  return a.subject === b.subject && a.body === b.body;
}

export interface LiveVerifyResult {
  /** The report to render: the live one while editing, else the committed one. */
  report: VerificationReport | null;
  verifying: boolean;
  error: string | null;
  /** True when `report` came from a dry run rather than from the stored item. */
  isLive: boolean;
  /** True when `report` describes exactly the pair currently on screen. */
  aligned: boolean;
}

/**
 * Re-verify an edited pair while the user types. `POST /verify/` is a dry run —
 * only `/edit/` persists. The server composes the draft from the pair, so the
 * pair that was sent is what identifies a reply: a response whose pair is no
 * longer the one on screen is an out-of-order debounced reply and is discarded,
 * never rendered against newer text.
 */
export function useLiveVerify(
  // Nullable so the hook call stays unconditional while the inbox loads.
  itemId: number | null,
  committedReport: VerificationReport | null,
  committed: CopyPair,
  draft: CopyPair,
  active: boolean,
): LiveVerifyResult {
  // The report and the pair it describes, kept together so they cannot drift.
  const [live, setLive] = useState<{ report: VerificationReport; pair: CopyPair } | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The fields' current values, readable from inside stale async closures.
  const draftRef = useRef(draft);
  draftRef.current = draft;

  // Monotonic request token: a superseded slow reply must not win.
  const latestRequest = useRef(0);

  // A new lead, or a fresh committed report from /edit/, resets the overlay.
  useEffect(() => {
    setLive(null);
    setError(null);
  }, [itemId, committedReport]);

  const untouched = samePair(draft, committed);

  useEffect(() => {
    if (!active) {
      // The editor closed with nothing pending: the dry run it produced
      // describes text no longer on screen, so it must not be rendered.
      setLive(null);
      setError(null);
      return;
    }
    if (itemId === null || committedReport === null) return;
    if (untouched) {
      // Back to what the server last verified — nothing to ask.
      setLive(null);
      setError(null);
      return;
    }

    const timer = window.setTimeout(() => {
      const token = (latestRequest.current += 1);
      const sent = draftRef.current;
      setVerifying(true);
      verifyCopy(itemId, sent)
        .then((response) => {
          if (token !== latestRequest.current) return;
          if (!samePair(sent, draftRef.current)) return; // stale
          setLive({ report: response, pair: sent });
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
  }, [active, draft.subject, draft.body, untouched, committedReport, itemId]);

  return {
    report: live?.report ?? committedReport,
    verifying,
    error,
    isLive: live !== null,
    aligned: live ? samePair(live.pair, draft) : untouched,
  };
}
