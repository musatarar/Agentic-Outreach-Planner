/** Model tier (e.g. flagship / balanced / fast), colored by tier name. */
export function TierBadge({ tier }: { tier: string }) {
  return <span className={`tier-badge tier-${tier}`}>{tier}</span>;
}
