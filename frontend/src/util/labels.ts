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
