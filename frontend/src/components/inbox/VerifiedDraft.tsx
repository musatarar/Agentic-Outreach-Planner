import { useEffect, useMemo } from 'react';
import type { VerificationClaim, VerificationReport } from '../../api/types';
import { type DraftSegment, misalignedClaims, splitDraftSegments } from './spans';

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

/** One field's runs. Claims keep the ids the summary links to. */
export function DraftRun({ segments }: { segments: DraftSegment[] }) {
  return (
    <>
      {segments.map((segment) => (
        <span
          key={segment.key}
          id={segment.claim ? `claim-span-${segment.claim.id}` : undefined}
          className={claimClassName(segment.claim)}
          title={claimTitle(segment.claim)}
        >
          {segment.text}
        </span>
      ))}
    </>
  );
}

export interface VerifiedDraftProps {
  /** The report to render; its `copy` is the text drawn — never local state. */
  report: VerificationReport;
}

/**
 * The draft as the two fields it is, with claims underlined: green = checked
 * against the lead record, red = not. The verifier indexes the composed draft,
 * so the underlines survive the split unchanged.
 */
export function VerifiedDraft({ report }: VerifiedDraftProps) {
  const parts = useMemo(() => splitDraftSegments(report), [report]);

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
    <div className="draft-fields">
      {parts.subject.length > 0 && (
        <div className="draft-field">
          <div className="draft-field__label">Subject</div>
          <div className="draft draft--subject">
            <DraftRun segments={parts.subject} />
          </div>
        </div>
      )}
      <div className="draft-field">
        <div className="draft-field__label">Body</div>
        <div className="draft">
          <DraftRun segments={parts.body} />
        </div>
      </div>
    </div>
  );
}
