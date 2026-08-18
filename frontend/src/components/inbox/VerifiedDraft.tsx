import { useEffect, useMemo } from 'react';
import type { VerificationClaim, VerificationReport } from '../../api/types';
import { buildDraftSegments, misalignedClaims } from './spans';

function claimClassName(claim: VerificationClaim | null): string | undefined {
  // `verified: null` (goal references, scheduling phrases) gets no underline.
  if (!claim || claim.verified === null) return undefined;
  return claim.verified ? 'claim claim--verified' : 'claim claim--unverified';
}

function claimTitle(claim: VerificationClaim | null): string | undefined {
  if (!claim) return undefined;
  if (claim.verified === true) return `Checked against ${claim.field}`;
  return claim.message || undefined;
}

export interface VerifiedDraftProps {
  /** The report to render; its `copy` is the text drawn — never local state. */
  report: VerificationReport;
  /** Overridden by the editor, which stacks this as an underlay layer. */
  className?: string;
}

/**
 * The draft with claims underlined: green = checked against the lead record,
 * red = not. Underlines are reserved for claims in generated copy.
 */
export function VerifiedDraft({ report, className = 'draft' }: VerifiedDraftProps) {
  const segments = useMemo(() => buildDraftSegments(report), [report]);

  // Astral canary: warn if offsets drift from the text they describe, rather
  // than silently underlining the wrong words.
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
    <div className={className}>
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
