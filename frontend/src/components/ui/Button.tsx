import type { MouseEventHandler, ReactNode } from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md';

export interface ButtonProps {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  disabled?: boolean;
  type?: 'button' | 'submit' | 'reset';
  onClick?: MouseEventHandler<HTMLButtonElement>;
  children?: ReactNode;
}

/**
 * The only button in the app. `primary` is the one accent fill on a screen —
 * if two primaries are visible at once, one of them is wrong.
 *
 * `loading` implies disabled, so a double-submit is impossible by construction,
 * and keeps the label mounted so the button does not resize mid-click.
 */
export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  disabled = false,
  type = 'button',
  onClick,
  children,
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`ui-button ui-button--${variant} ui-button--${size}`}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      onClick={onClick}
    >
      {loading && <span className="ui-button__spinner" aria-hidden="true" />}
      {children}
    </button>
  );
}
