import { useEffect, useRef } from 'react';

/**
 * Global keyboard shortcuts.
 *
 * MUS-40 owns this file, but it is a **shared dependency**: CONTRACT §9.13
 * requires MUS-38 and MUS-41 to bind through it rather than adding their own
 * `keydown` listeners, because the guard below is the thing that stops typing
 * "snooze" into an email field from firing S, X and E.
 *
 * The model, deliberately small:
 *
 *   - A binding is a lower-cased key, optionally prefixed with `mod+` (⌘ on
 *     macOS, Ctrl elsewhere) and/or `alt+`: `'j'`, `'?'`, `'escape'`,
 *     `'arrowdown'`, `'mod+enter'`.
 *   - **Shift is never part of a combo.** `event.key` already reflects it —
 *     Shift+/ arrives as `'?'` — and folding Shift+J into `'j'` is what a user
 *     holding shift by accident expects.
 *   - Because modifiers are part of the combo, `'a'` does not match ⌘A, so
 *     select-all and the rest of the browser's chords keep working.
 *   - `'?'` therefore requires no modifiers by construction: ⌘? normalises to
 *     `'mod+?'`, which no binding claims.
 */

export type HotkeyHandler = (event: KeyboardEvent) => void;
export type HotkeyMap = Record<string, HotkeyHandler>;

export interface UseHotkeysOptions {
  /** Bindings are ignored while false — used to mute the page under a modal. */
  enabled?: boolean;
}

const TEXT_ENTRY_SELECTOR = 'input, textarea, select, [contenteditable="true"]';

/**
 * The two combos that survive a focused text field, because the inline editor
 * needs them: Esc discards, ⌘/Ctrl+Enter commits.
 */
const TEXT_ENTRY_ALLOWED = new Set(['escape', 'mod+enter']);

/** Does this binding survive a focused text field? Exported to be asserted. */
export function isAllowedInTextEntry(combo: string): boolean {
  return TEXT_ENTRY_ALLOWED.has(combo);
}

/** Is the caret currently somewhere that swallows plain letters? */
export function isTextEntryTarget(element: Element | null): boolean {
  return element instanceof HTMLElement && element.matches(TEXT_ENTRY_SELECTOR);
}

/** Normalise a keydown into a binding string. */
export function hotkeyCombo(event: KeyboardEvent): string {
  const parts: string[] = [];
  if (event.metaKey || event.ctrlKey) parts.push('mod');
  if (event.altKey) parts.push('alt');
  parts.push(event.key.toLowerCase());
  return parts.join('+');
}

export function useHotkeys(map: HotkeyMap, options: UseHotkeysOptions = {}): void {
  const { enabled = true } = options;

  // Handlers close over fresh state every render; the listener is bound once.
  const latest = useRef(map);
  latest.current = map;

  useEffect(() => {
    if (!enabled) return;

    function onKeyDown(event: KeyboardEvent) {
      // Something closer to the source already claimed this key — the inline
      // editor's own Esc and ⌘⏎ handlers, for instance.
      if (event.defaultPrevented) return;
      // Mid-IME composition, every keystroke is text, never a command.
      if (event.isComposing) return;

      const combo = hotkeyCombo(event);

      // CONTRACT §9.13. The whole point of this hook.
      if (isTextEntryTarget(document.activeElement) && !TEXT_ENTRY_ALLOWED.has(combo)) {
        return;
      }

      const handler = latest.current[combo];
      if (!handler) return;

      event.preventDefault();
      handler(event);
    }

    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [enabled]);
}
