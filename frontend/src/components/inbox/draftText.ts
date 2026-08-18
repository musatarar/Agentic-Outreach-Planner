/**
 * Splits the draft's "Subject:" prefix off the prose so the two can take
 * different type families. Pure ASCII, so its length is identical in code
 * points and UTF-16 units — safe against either indexing scheme.
 */
export const SUBJECT_PREFIX = 'Subject:';

/** How many leading characters of `copy` are the sans label, or 0 if none. */
export function subjectLabelLength(copy: string): number {
  return copy.startsWith(SUBJECT_PREFIX) ? SUBJECT_PREFIX.length : 0;
}
