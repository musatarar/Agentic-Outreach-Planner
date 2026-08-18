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
 * A surface on the canvas: `flat` is border-only, `raised` adds one shadow.
 * `as` lets a card be an <article>/<li>/<section> without a wrapper div
 * swallowing the semantics.
 */
export function Card({ elevation = 'flat', padding = 'md', as, children }: CardProps) {
  const Tag: ElementType = as ?? 'div';
  return (
    <Tag className={`ui-card ui-card--${elevation} ui-card--pad-${padding}`}>{children}</Tag>
  );
}
