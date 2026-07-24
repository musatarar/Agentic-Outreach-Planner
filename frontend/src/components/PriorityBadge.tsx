import type { Priority } from '../api/types';

/** P1 red / P2 orange-yellow / P3 gray, matching the old templates. */
export function PriorityBadge({
  priority,
  variant = 'badge',
}: {
  priority: Priority;
  variant?: 'badge' | 'priority-badge';
}) {
  return <span className={`${variant} p${priority}`}>P{priority}</span>;
}
