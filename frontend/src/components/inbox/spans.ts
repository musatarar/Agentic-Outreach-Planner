import type { VerificationClaim, VerificationReport } from '../../api/types';
import { subjectLabelLength } from './draftText';

/**
 * Turns a verification report into renderable runs of draft text. Pure, no JSX.
 * Offsets are Unicode code-point indices (Python `re`), not UTF-16 units;
 * when `is_astral_safe` is false, slice a code-point array instead. Spans
 * arrive pre-trimmed, and the rendered text is always `report.copy` — the
 * exact string the offsets index into.
 */

export type SegmentRole = 'text' | 'subject-label';

export interface DraftSegment {
  key: string;
  text: string;
  /** Non-null when this run is a claim and should carry an underline. */
  claim: VerificationClaim | null;
  role: SegmentRole;
}

/** A code-point-safe slicer over one report's copy. */
function sliceFor(report: VerificationReport) {
  // Array.from splits on code points, not UTF-16 units.
  const units = report.is_astral_safe ? null : Array.from(report.copy);
  const length = units ? units.length : report.copy.length;
  const cut = (start: number, end: number) =>
    units ? units.slice(start, end).join('') : report.copy.slice(start, end);
  return { cut, length };
}

/** A claim with real offsets. Omission claims carry null and are excluded. */
type SpannedClaim = VerificationClaim & { start: number; end: number };

/** Claims that carry a span, in render order. */
function spannedClaims(report: VerificationReport): SpannedClaim[] {
  return report.claims.filter(
    (claim): claim is SpannedClaim =>
      claim.start !== null && claim.end !== null && claim.end > claim.start,
  );
}

/**
 * Split `report.copy` into runs, each either plain text or one claim.
 * Overlapping claims are dropped rather than nested.
 */
export function buildDraftSegments(report: VerificationReport): DraftSegment[] {
  const { cut, length } = sliceFor(report);
  const segments: DraftSegment[] = [];
  let cursor = 0;

  // "Subject:" prefix is ASCII-only, so its length matches in both index schemes.
  const labelLength = subjectLabelLength(report.copy);
  if (labelLength > 0 && labelLength <= length) {
    segments.push({
      key: 'subject-label',
      text: cut(0, labelLength),
      claim: null,
      role: 'subject-label',
    });
    cursor = labelLength;
  }

  for (const claim of spannedClaims(report)) {
    if (claim.start < cursor || claim.end > length) continue;
    if (claim.start > cursor) {
      segments.push({
        key: `text-${cursor}`,
        text: cut(cursor, claim.start),
        claim: null,
        role: 'text',
      });
    }
    segments.push({
      key: claim.id,
      text: cut(claim.start, claim.end),
      claim,
      role: 'text',
    });
    cursor = claim.end;
  }

  if (cursor < length) {
    segments.push({
      key: `text-${cursor}`,
      text: cut(cursor, length),
      claim: null,
      role: 'text',
    });
  }

  return segments;
}

/** Self-check: every spanned claim must slice back to its own `text`. */
export function misalignedClaims(report: VerificationReport): VerificationClaim[] {
  const { cut } = sliceFor(report);
  return spannedClaims(report).filter((claim) => cut(claim.start, claim.end) !== claim.text);
}

/**
 * Which claim is stopping approval. Offers don't count toward the `N of M`
 * ratio, so "4 of 4 verified" can still be blocked; offers surface first.
 */
export function findBlockingClaim(report: VerificationReport): VerificationClaim | null {
  return (
    report.claims.find((claim) => claim.kind === 'unauthorized_offer') ??
    report.claims.find((claim) => claim.verified === false && claim.counts_toward_summary) ??
    report.claims.find((claim) => claim.verified === false) ??
    null
  );
}

export type BlockerCause = 'unauthorized_offer' | 'unverified_claim' | 'unknown';

export function blockerCause(claim: VerificationClaim | null): BlockerCause {
  if (!claim) return 'unknown';
  return claim.kind === 'unauthorized_offer' ? 'unauthorized_offer' : 'unverified_claim';
}
