import { useTheme } from '../../hooks/useTheme';

/**
 * The dark/light control for the app shell. Not one of the five frozen UI
 * primitives, but it is the only consumer of useTheme and it has to live
 * somewhere every shell can import it from, so it ships beside them and is
 * exported from the same barrel.
 *
 * Flipping the attribute on <html> re-resolves every token at once, so the
 * whole app changes in a single paint with no transition to wait on.
 */
export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const next = theme === 'dark' ? 'light' : 'dark';

  return (
    <button
      type="button"
      className="ui-theme-toggle"
      onClick={toggleTheme}
      title={`Switch to ${next} theme`}
      aria-label={`Switch to ${next} theme`}
    >
      <span aria-hidden="true">{theme === 'dark' ? '☾' : '☀'}</span>
      <span className="ui-theme-toggle__label">{next}</span>
    </button>
  );
}
