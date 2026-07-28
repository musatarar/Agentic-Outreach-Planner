import { useEffect, useRef } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';

export interface PopoverProps {
  label: string;
  onClose: () => void;
  children: ReactNode;
}

/**
 * A small anchored panel: snooze, dismiss.
 *
 * Focus moves in on open and returns to whatever had it on close, so a
 * keyboard user is never dumped at the top of the document after picking an
 * option. Esc closes, and so does a click outside — both are handled here so
 * neither popover has to remember.
 */
export function Popover({ label, onClose, children }: PopoverProps) {
  const panel = useRef<HTMLDivElement | null>(null);
  const restoreTo = useRef<HTMLElement | null>(null);

  useEffect(() => {
    restoreTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const first = panel.current?.querySelector<HTMLElement>('button, [href], input, select');
    first?.focus();
    return () => restoreTo.current?.focus();
  }, []);

  useEffect(() => {
    function onPointerDown(event: MouseEvent) {
      if (!panel.current?.contains(event.target as Node)) onClose();
    }
    // Bound on the next tick so the click that opened the popover does not
    // immediately close it again.
    const timer = window.setTimeout(
      () => document.addEventListener('mousedown', onPointerDown),
      0,
    );
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('mousedown', onPointerDown);
    };
  }, [onClose]);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    }
  }

  return (
    <div
      ref={panel}
      className="popover"
      role="dialog"
      aria-label={label}
      aria-modal="false"
      onKeyDown={onKeyDown}
    >
      {children}
    </div>
  );
}

export interface MenuOption<T extends string> {
  value: T;
  label: string;
  /** The quiet second line — what the option actually does. */
  hint?: string;
}

export interface MenuListProps<T extends string> {
  options: MenuOption<T>[];
  onSelect: (value: T) => void;
}

/**
 * A vertical list of choices with roving arrow-key focus.
 *
 * Real `<button>`s rather than a custom widget, so Enter and Space activate
 * natively and every assistive technology already knows what they are.
 */
export function MenuList<T extends string>({ options, onSelect }: MenuListProps<T>) {
  const list = useRef<HTMLDivElement | null>(null);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    const buttons = Array.from(list.current?.querySelectorAll('button') ?? []);
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (at === -1) return;
    event.preventDefault();
    const step = event.key === 'ArrowDown' ? 1 : -1;
    // Wraps, because a five-item menu is short enough that wrapping is faster
    // than noticing you hit the end.
    buttons[(at + step + buttons.length) % buttons.length]?.focus();
  }

  return (
    <div ref={list} className="popover__menu" onKeyDown={onKeyDown}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className="popover__item"
          onClick={() => onSelect(option.value)}
        >
          <span className="popover__item-label">{option.label}</span>
          {option.hint && <span className="popover__item-hint">{option.hint}</span>}
        </button>
      ))}
    </div>
  );
}
