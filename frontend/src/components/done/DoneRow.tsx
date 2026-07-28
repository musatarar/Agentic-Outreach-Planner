import type { QueueItem } from '../../api/types';
import { Badge } from '../ui';
import { formatTimeOfDay } from './format';
import { STATUS_LABELS, describeOutcome } from './labels';

export interface DoneRowProps {
  item: QueueItem;
  /** `DoneResponse.timezone` — the clock the server decided the day in. */
  timeZone: string;
}

/**
 * One thing you did today: who, where, what it was, what happened, when.
 *
 * The timestamp is `status_changed_at` — when you actioned it, not when the
 * planner created it. `created_at` is only a fallback so a row can never
 * render a blank time.
 */
export function DoneRow({ item, timeZone }: DoneRowProps) {
  const actionedAt = item.status_changed_at ?? item.created_at;

  return (
    <li className={`done-row done-row--${item.status}`}>
      <div className="done-row__status">
        <Badge tone={item.status}>{STATUS_LABELS[item.status]}</Badge>
      </div>

      <div className="done-row__body">
        <p className="done-row__who">
          <span className="done-row__contact">{item.lead.contact_name}</span>
          <span className="done-row__agency">{item.lead.agency_name}</span>
        </p>
        {/* action_label is server-rendered prose for the action type; the FE
            never reconstructs it from action_type. */}
        <p className="done-row__action">{item.action_label}</p>
        <p className="done-row__outcome">{describeOutcome(item, timeZone)}</p>
      </div>

      <div className="done-row__aside">
        <time className="done-row__time" dateTime={actionedAt}>
          {formatTimeOfDay(actionedAt, timeZone)}
        </time>
      </div>
    </li>
  );
}
