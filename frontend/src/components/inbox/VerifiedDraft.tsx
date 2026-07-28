import { useEffect, useMemo } from 'react';
import type { VerificationClaim, VerificationReport } from '../../api/types';
import { buildDraftSegments, misalignedClaims } from './spans';

function claimClassName(claim: VerificationClaim | null): string | undefined {
  // `verified: null` is neither grounded nor contradicted — a goal reference or
  // a scheduling phrase. It gets no underline at all, because an underline the
  // user cannot act on dilutes the two that they can.
  if (!claim || claim.verified === null) return undefined;
  return claim.verified ? 'claim claim--verified' : 'claim claim--unverified';
}

function claimTitle(claim: VerificationClaim | null): string | undefined {
  if (!claim) return undefined;
  if (claim.verified === true) return `Checked against ${claim.field}`;
  return claim.message || undefined;
}

export interface VerifiedDraftProps {
  /**
   * The report to render. Its `copy` field is the text drawn — not local
   * state, not `effective_copy` (CONTRACT §9.2). During live editing this is
   * the response from `/verify/`, which echoes the exact string it verified.
   */
  report: VerificationReport;
}

/**
 * The draft, in serif, with the claims underlined.
 *
 * The underlines are the highest-trust element in the design. Green means this
 * number was checked against the lead record; red means it was not. Together
 * they tell the user exactly which parts of the email they do not need to
 * fact-check, which is the entire reason to trust generated copy at all.
 *
 * They are reserved for claims inside generated copy: never a fill, never on
 * chrome, never anywhere but here.
 */
export function VerifiedDraft({ report }: VerifiedDraftProps) {
  const segments = useMemo(() => buildDraftSegments(report), [report]);

  // The astral canary. If offsets ever stop lining up with the text they
  // describe, every underline after the first emoji lands on the wrong words —
  // for one lead, in production data only, where no fixture will catch it.
  // Better a console line than a silent lie about what was verified.
  useEffect(() => {
    const drifted = misalignedClaims(report);
    if (drifted.length > 0) {
      console.warn(
        '[inbox] verification spans do not match their claim text',
        drifted.map((claim) => claim.id),
      );
    }
  }, [report]);

  return (
    <div className="draft">
      {segments.map((segment) =>
        segment.role === 'subject-label' ? (
          <span key={segment.key} className="draft__subject-label">
            {segment.text}
          </span>
        ) : (
          <span
            key={segment.key}
            id={segment.claim ? `claim-span-${segment.claim.id}` : undefined}
            className={claimClassName(segment.claim)}
            title={claimTitle(segment.claim)}
          >
            {segment.text}
          </span>
        ),
      )}
    </div>
  );
}
