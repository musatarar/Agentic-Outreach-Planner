/**
 * The Locked In mark. Inline SVG so it can take `currentColor` and
 * `--color-accent` and invert with the theme. The same geometry is duplicated
 * with hardcoded colours in project/app/static/brand/favicon.svg — change both.
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
