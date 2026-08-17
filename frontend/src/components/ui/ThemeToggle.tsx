import { useTheme } from '../../hooks/useTheme';

/**
 * The dark/light control for the app shell. Flipping the attribute on <html>
 * re-resolves every token at once, so the app changes in a single paint.
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
