/** Human-readable names for the action types produced by services/outreach.py. */
const ACTION_LABELS: Record<string, string> = {
  power_user_reward: 'Power User Reward',
  follow_up_after_hold: 'Follow Up After Hold',
  reengage_dormant: 'Re-engage Dormant',
  nudge_usage: 'Nudge Usage',
  complete_onboarding: 'Complete Onboarding',
  unknown: 'Needs BD Review',
};

export function formatActionType(actionType: string): string {
  return ACTION_LABELS[actionType] ?? actionType;
}

export function formatTimestamp(isoDate: string): string {
  return new Date(isoDate).toLocaleString();
}

export function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max).trimEnd()}…` : text;
}

const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

/**
 * Format a DRF DateField ("YYYY-MM-DD", or null) for display.
 *
 * Deliberately string surgery rather than `new Date(value)`: that parses a
 * date-only string as UTC midnight, so every date renders a day early in any
 * negative-offset timezone. Use `formatTimestamp` for real datetimes, where the
 * instant genuinely is the point.
 */
export function formatDateOnly(value: string | null): string {
  if (!value) return 'Never';
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  const [, year, month, day] = match;
  const name = MONTHS[Number(month) - 1];
  return name ? `${name} ${Number(day)}, ${year}` : value;
}

/**
 * Pipeline stage for display: `active_trial` → `Active trial`.
 *
 * A transform rather than a lookup table, so a stage the backend adds later
 * still renders as readable prose instead of falling through to a raw slug.
 */
export function formatStage(stage: string): string {
  const words = stage.replace(/_/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : stage;
}

/** Book size for a table cell: `$2M`, `$1.3M`, `$900K`. */
export function formatUsdCompact(value: number): string {
  if (value >= 1_000_000) {
    const millions = value / 1_000_000;
    const text = millions.toFixed(1);
    return `$${text.endsWith('.0') ? text.slice(0, -2) : text}M`;
  }
  if (value >= 1_000) return `$${Math.round(value / 1_000)}K`;
  return `$${value}`;
}
