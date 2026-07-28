import { useCallback, useEffect, useState } from 'react';

export type Theme = 'light' | 'dark';

/** Same key the boot script in spa_base.html reads. Do not rename one alone. */
const STORAGE_KEY = 'theme';
const DARK_QUERY = '(prefers-color-scheme: dark)';

function readStored(): Theme | null {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === 'light' || stored === 'dark' ? stored : null;
  } catch {
    // Safari private mode / storage disabled: fall through to the OS preference.
    return null;
  }
}

function writeStored(theme: Theme): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Non-fatal: the attribute still flips, the choice just won't survive reload.
  }
}

function systemTheme(): Theme {
  return window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light';
}

/**
 * Resolve the theme the same way the boot script did, in the same precedence
 * order (CONTRACT §7.3): explicit localStorage > prefers-color-scheme > light.
 *
 * The attribute is read first so React starts from whatever is already painted
 * — that is what keeps the first render flash-free.
 */
function initialTheme(): Theme {
  const attr = document.documentElement.getAttribute('data-theme');
  if (attr === 'light' || attr === 'dark') return attr;
  return readStored() ?? systemTheme();
}

/**
 * Dark/light theming. Writes `data-theme` on <html> and persists the choice.
 * Never removes the attribute — the stylesheet's OS-preference block is a
 * no-JS fallback and must not be reachable once React has mounted.
 */
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  // Follow the OS only while the user has made no explicit choice.
  useEffect(() => {
    const media = window.matchMedia(DARK_QUERY);
    const onChange = (event: MediaQueryListEvent) => {
      if (readStored() === null) setThemeState(event.matches ? 'dark' : 'light');
    };
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);

  const setTheme = useCallback((next: Theme) => {
    writeStored(next);
    setThemeState(next);
  }, []);

  const toggleTheme = useCallback(() => {
    setThemeState((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark';
      writeStored(next);
      return next;
    });
  }, []);

  return { theme, setTheme, toggleTheme };
}
