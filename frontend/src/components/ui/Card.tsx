import type { ElementType, ReactNode } from 'react';

export type CardElevation = 'flat' | 'raised';
export type CardPadding = 'sm' | 'md' | 'lg';

export interface CardProps {
  elevation?: CardElevation;
  padding?: CardPadding;
  as?: ElementType;
  children?: ReactNode;
}

/**
 * A surface on the canvas. The contrast between --color-bg and
 * --color-surface is where the boldness comes from, so `flat` (border only)
 * is the default and `raised` adds one restrained shadow. No gradients, no
 * glows — if a card needs decoration to read as a card, the canvas is wrong.
 *
 * `as` exists so a card can be an <article>, <li> or <section> without a
 * wrapper div swallowing the list semantics.
 */
export function Card({ elevation = 'flat', padding = 'md', as, children }: CardProps) {
  const Tag: ElementType = as ?? 'div';
  return (
    <Tag className={`ui-card ui-card--${elevation} ui-card--pad-${padding}`}>{children}</Tag>
  );
}
