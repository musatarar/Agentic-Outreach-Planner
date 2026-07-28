import type { ReactNode } from 'react';

export type BadgeTone =
  | 'p1'
  | 'p2'
  | 'p3'
  | 'accent'
  | 'neutral'
  | 'verified'
  | 'unverified'
  | 'pending'
  | 'approved'
  | 'snoozed'
  | 'dismissed';

export interface BadgeProps {
  tone: BadgeTone;
  children?: ReactNode;
}

/**
 * Small status chip. Tones map straight onto the token ramps, so nothing here
 * chooses a colour — the ramp does.
 *
 * Two rules the tones encode:
 *   - p1/p2 are the only red and amber in the app.
 *   - verified/unverified render as a neutral chip with a verification
 *     *underline*, never a green or red fill. The verification ramp is the
 *     highest-trust element in the design and it only ever underlines.
 */
export function Badge({ tone, children }: BadgeProps) {
  return <span className={`ui-badge ui-badge--${tone}`}>{children}</span>;
}
