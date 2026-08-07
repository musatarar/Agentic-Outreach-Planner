import type { VerificationClaim, VerificationReport } from '../../api/types';
import { subjectLabelLength } from './draftText';

/**
 * Turning a verification report into renderable runs of draft text.
 *
 * Pure, and deliberately kept free of JSX so it can be exercised on its own.
 *
 * Three edges.2 live here:
 *
 *  (a) Offsets are Unicode CODE POINT indices, because Python's `re` yields
 *      those. JavaScript's `String.prototype.slice` counts UTF-16 code units,
 *      so a single emoji anywhere in the copy shifts every later underline by
 *      one in JS and by zero in Python. `is_astral_safe` is the server telling
 *      us the two agree; when it is false we index a code-point array instead.
 *      This is per-lead, production-data-only breakage — it will never show up
 *      on the golden fixtures — so it is handled unconditionally rather than
 *      when someone notices.
 *
 *  (b) Spans are already whitespace-trimmed server-side, so nothing here
 *      re-trims and shifts them back out of alignment.
 *
 *  (c) The text rendered is `report.copy` — the exact string the offsets index
 *      into — never a local copy of the draft. During live editing the report
 *      comes from `/verify/`, which echoes what it verified.
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
  // Array.from splits on code points, so an astral character is one element
  // rather than a surrogate pair.
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
 *
 * Claims arrive ordered by start; overlapping ones are dropped rather than
 * nested, because a nested underline cannot be read as either colour and an
 * exception here would blank the whole draft.
 */
export function buildDraftSegments(report: VerificationReport): DraftSegment[] {
  const { cut, length } = sliceFor(report);
  const segments: DraftSegment[] = [];
  let cursor = 0;

  // The literal "Subject:" prefix is a sans label; the subject after it is
  // voice. ASCII-only, so its length is the same in both index schemes.
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

/**
 * Self-check for the astral case: every spanned claim must slice back to its
 * own `text`. Exported so it can be asserted against a fixture, and called in
 * dev below — a silent one-character drift is otherwise invisible until a user
 * reports that the wrong words are underlined.
 */
export function misalignedClaims(report: VerificationReport): VerificationClaim[] {
  const { cut } = sliceFor(report);
  return spannedClaims(report).filter((claim) => cut(claim.start, claim.end) !== claim.text);
}

/**
 * Which claim is stopping approval.
 *
 * `can_approve` has two independent causes that
 * compose — no unverified claims, AND no `unauthorized_offer` claim. An offer
 * does not count toward the `N of M` ratio, so a draft can legitimately read
 * `4 of 4 claims verified` and still be blocked. That is not a bug to
 * reconcile: the ratio and the gate answer different questions.
 *
 * Offers are surfaced first because promising a customer something the company
 * has not authorised is the more consequential of the two.
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
