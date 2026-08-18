import { useEffect, useRef } from 'react';

/**
 * Global keyboard shortcuts — every page binds through this so the text-field
 * guard applies. A binding is a lower-cased key, optionally prefixed with
 * `mod+` (⌘/Ctrl) and/or `alt+`: 'j', '?', 'escape', 'mod+enter'. Shift is
 * never part of a combo — `event.key` already reflects it (Shift+/ is '?') —
 * and since modifiers are part of the combo, 'a' does not match ⌘A.
 */

export type HotkeyHandler = (event: KeyboardEvent) => void;
export type HotkeyMap = Record<string, HotkeyHandler>;

export interface UseHotkeysOptions {
  /** Bindings are ignored while false — used to mute the page under a modal. */
  enabled?: boolean;
}

const TEXT_ENTRY_SELECTOR = 'input, textarea, select, [contenteditable="true"]';

/** Combos that survive a focused text field: Esc discards, ⌘/Ctrl+Enter commits. */
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
      // A closer handler (e.g. the inline editor) already claimed this key.
      if (event.defaultPrevented) return;
      // Mid-IME composition, every keystroke is text, never a command.
      if (event.isComposing) return;

      const combo = hotkeyCombo(event);

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
