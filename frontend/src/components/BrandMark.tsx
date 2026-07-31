/**
 * The Locked In mark: four corner brackets closing in on a single square.
 *
 * Drawn inline rather than shipped as an asset so the brackets can take
 * `currentColor` and the square `--color-accent` — the mark inverts with the
 * theme for free, and Vite never has to hash a file that `spa_base.html` would
 * then need a manifest to find. Using the accent token rather than the literal
 * logo orange keeps the no-hex-outside-tokens.css rule intact, and is what
 * makes the square legible on the dark canvas.
 *
 * The standalone copy at project/app/static/brand/favicon.svg is this same
 * geometry with the colours hardcoded, because a favicon has no cascade to
 * read tokens from; change one and change the other.
 */
export function BrandMark({ size = 18 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
    >
      <g fill="none" stroke="currentColor" strokeWidth="2.75" strokeLinecap="square">
        <path d="M5.375 12.5V5.375H12.5" />
        <path d="M19.5 5.375h7.125V12.5" />
        <path d="M26.625 19.5v7.125H19.5" />
        <path d="M12.5 26.625H5.375V19.5" />
      </g>
      <rect x="14.75" y="14.75" width="2.5" height="2.5" fill="var(--color-accent)" />
    </svg>
  );
}
