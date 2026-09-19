import type { VerificationClaim, VerificationReport } from '../../api/types';

/**
 * Turns a verification report into renderable runs of draft text. Pure, no JSX.
 * Offsets are Unicode code-point indices (Python `re`), not UTF-16 units;
 * when `is_astral_safe` is false, slice a code-point array instead. Spans
 * arrive pre-trimmed, and the rendered text is always `report.copy` — the
 * exact string the offsets index into.
 */

export interface DraftSegment {
  key: string;
  text: string;
  /** Non-null when this run is a claim and should carry an underline. */
  claim: VerificationClaim | null;
}

/** One report's segments, partitioned into the two fields the inbox renders. */
export interface DraftParts {
  /** Empty when the draft carries no `Subject:` line. */
  subject: DraftSegment[];
  body: DraftSegment[];
}

/* ----------------------------------------------------------------------
   Where the subject ends and the body begins. The server composes every draft
   as `Subject: <subject>\n\n<body>` (services/outreach.py `compose_email`) and
   the verifier's offsets index that whole string, so rendering the two halves
   separately means partitioning those offsets rather than asking for two sets.
   ---------------------------------------------------------------------- */

export const SUBJECT_PREFIX = 'Subject:';

/**
 * The draft a pair composes to, for the CLIPBOARD ONLY -- what a reviewer
 * copies should be what is on screen, and on screen it is two fields.
 *
 * The server owns the composition that is stored and verified
 * (`compose_email`); this never decides either, so if the two ever drifted the
 * symptom would be a pasted string differing from the stored one, never a draft
 * stored or approved against the wrong text. Approval is gated on a report that
 * describes the pair exactly, where this and `report.copy` agree by definition.
 */
export function composeDraft(pair: { subject: string; body: string }): string {
  return `${SUBJECT_PREFIX} ${pair.subject.split(/\s+/).filter(Boolean).join(' ')}\n\n${pair.body.trim()}`;
}

/** The blank line the composer puts between the subject and the body. */
const SEPARATOR = '\n\n';

export interface DraftBounds {
  /** False when the draft has no `Subject:` line — then it is all body. */
  hasSubject: boolean;
  subjectStart: number;
  subjectEnd: number;
  bodyStart: number;
}

/**
 * Offsets into `copy` in whichever scheme the claims use: pass `units` (the
 * code-point array) when the report is not astral-safe, `null` otherwise. An
 * emoji in the subject moves the separator by one UTF-16 unit but not by one
 * code point, so searching the wrong sequence would cut the draft in the wrong
 * place — the one difference between the two schemes that matters here.
 */
export function draftBounds(copy: string, units: string[] | null): DraftBounds {
  const length = units ? units.length : copy.length;
  if (!copy.startsWith(SUBJECT_PREFIX)) {
    return { hasSubject: false, subjectStart: 0, subjectEnd: 0, bodyStart: 0 };
  }
  // The composer writes exactly one space after the label; the label itself is
  // ASCII, so its length is the same in both schemes.
  const subjectStart = SUBJECT_PREFIX.length + (copy[SUBJECT_PREFIX.length] === ' ' ? 1 : 0);
  const separator = units
    ? units.findIndex(
        (unit, index) => index >= subjectStart && unit === '\n' && units[index + 1] === '\n',
      )
    : copy.indexOf(SEPARATOR, subjectStart);
  if (separator === -1) {
    // A subject line and nothing under it: no body to render.
    return { hasSubject: true, subjectStart, subjectEnd: length, bodyStart: length };
  }
  return {
    hasSubject: true,
    subjectStart,
    subjectEnd: separator,
    bodyStart: separator + SEPARATOR.length,
  };
}

/** A code-point-safe slicer over one report's copy. */
function sliceFor(report: VerificationReport) {
  // Array.from splits on code points, not UTF-16 units.
  const units = report.is_astral_safe ? null : Array.from(report.copy);
  const length = units ? units.length : report.copy.length;
  const cut = (start: number, end: number) =>
    units ? units.slice(start, end).join('') : report.copy.slice(start, end);
  return { cut, length, units };
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
 * Split the window `[from, to)` of `report.copy` into runs, each either plain
 * text or one claim. Overlapping claims are dropped rather than nested; a claim
 * straddling the window's edge is clipped to it, so it stays underlined in both
 * halves rather than vanishing from one.
 */
export function segmentsIn(
  report: VerificationReport,
  from: number,
  to: number,
): DraftSegment[] {
  const { cut } = sliceFor(report);
  const segments: DraftSegment[] = [];
  let cursor = from;

  for (const claim of spannedClaims(report)) {
    const start = Math.max(claim.start, from);
    const end = Math.min(claim.end, to);
    if (end <= start || start < cursor) continue;
    if (start > cursor) {
      segments.push({ key: `text-${cursor}`, text: cut(cursor, start), claim: null });
    }
    segments.push({ key: `${claim.id}-${start}`, text: cut(start, end), claim });
    cursor = end;
  }

  if (cursor < to) {
    segments.push({ key: `text-${cursor}`, text: cut(cursor, to), claim: null });
  }
  return segments;
}

/** Every run of `report.copy`, in one list. */
export function buildDraftSegments(report: VerificationReport): DraftSegment[] {
  const { length } = sliceFor(report);
  return segmentsIn(report, 0, length);
}

/**
 * The report's runs split at the composer's own boundary, so the subject and
 * the body can be rendered as the two fields they are while their claims keep
 * the offsets the verifier computed over the composed draft.
 */
export function splitDraftSegments(report: VerificationReport): DraftParts {
  const { length, units } = sliceFor(report);
  const bounds = draftBounds(report.copy, units);
  return {
    subject: bounds.hasSubject ? segmentsIn(report, bounds.subjectStart, bounds.subjectEnd) : [],
    body: segmentsIn(report, bounds.bodyStart, length),
  };
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
