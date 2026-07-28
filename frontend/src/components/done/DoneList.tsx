import type { QueueItem } from '../../api/types';
import { DoneRow } from './DoneRow';
import { dayKey, formatDayLabel } from './format';

export interface DoneListProps {
  /** Server order — `status_changed_at DESC`. Never re-sorted here. */
  items: QueueItem[];
  timeZone: string;
}

interface DayBucket {
  key: string;
  items: QueueItem[];
}

/**
 * Bucket by calendar day *in the server's triage zone*, preserving the
 * server's reverse-chronological order within and across buckets.
 *
 * Grouping on the browser's calendar day would split one evening triage
 * session across two headings for anyone west of UTC — the same class of bug
 * CONTRACT §9.5 pins for the page date.
 */
function groupByDay(items: QueueItem[], timeZone: string): DayBucket[] {
  const buckets: DayBucket[] = [];
  for (const item of items) {
    const key = dayKey(item.status_changed_at ?? item.created_at, timeZone);
    const last = buckets[buckets.length - 1];
    if (last && last.key === key) {
      last.items.push(item);
    } else {
      buckets.push({ key, items: [item] });
    }
  }
  return buckets;
}

/**
 * The record of the day. `/api/queue/done/` returns today only, so in practice
 * there is one bucket and the heading is suppressed — repeating the date
 * already in the page header would be noise. The grouping is here so that the
 * day the range widens, the headings appear with no further work.
 */
export function DoneList({ items, timeZone }: DoneListProps) {
  const days = groupByDay(items, timeZone);
  const showHeadings = days.length > 1;

  return (
    <>
      {days.map((day) => (
        <section className="done-day" key={day.key}>
          {showHeadings && (
            <h2 className="done-day__heading">
              <span className="done-day__label">{formatDayLabel(day.key)}</span>
              <span className="done-day__key">{day.key}</span>
            </h2>
          )}
          <ul className="done-list">
            {day.items.map((item) => (
              <DoneRow key={item.id} item={item} timeZone={timeZone} />
            ))}
          </ul>
        </section>
      ))}
    </>
  );
}
