/**
 * Splitting the draft's "Subject:" prefix off the prose.
 *
 * The label is chrome and the subject it introduces is voice, so they take
 * different type families — sans and serif — even though they are one string
 * in the payload. The split is by literal prefix rather than by parsing, so
 * copy that does not start with a subject line is simply left whole.
 *
 * The prefix is pure ASCII, so its length is identical in code points and in
 * UTF-16 code units; this offset is safe to use against either indexing scheme
 * (see CONTRACT §9.1a).
 */
export const SUBJECT_PREFIX = 'Subject:';

/** How many leading characters of `copy` are the sans label, or 0 if none. */
export function subjectLabelLength(copy: string): number {
  return copy.startsWith(SUBJECT_PREFIX) ? SUBJECT_PREFIX.length : 0;
}
