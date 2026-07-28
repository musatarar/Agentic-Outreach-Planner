import type { DoneResponse } from '../../api/types';
import {
  formatDayLabel,
  formatDuration,
  formatHourMinute,
  formatUsdCompact,
} from './format';

export interface QueueClearedProps {
  data: DoneResponse;
}

interface Segment {
  key: string;
  count: number;
}

/**
 * `summary.queue_cleared === true`. **A server flag, never inferred** — §5.2
 * defines it as `counts.remaining === 0 && summary.total > 0`, and the FE has
 * no honest way to know `remaining`. Deriving it from `items.length` would
 * light this screen up while leads were still waiting.
 *
 * This is the one moment in the product where the user feels good about the
 * software, so it gets the loudest thing the design system can say: the
 * status-approved pair, inverted. In light mode that is near-black on the
 * canvas; in dark mode it flips to near-white. It is the same token pair every
 * approved chip uses, at full page scale — "resolved", said as loudly as the
 * palette permits, without spending the accent or inventing a colour.
 *
 * Every figure is mono. They are counts, money and durations: machine output,
 * and the type discipline in §7.1 is the most distinctive thing in this design.
 */
export function QueueCleared({ data }: QueueClearedProps) {
  const { summary, timezone } = data;

  const segments: Segment[] = [
    { key: 'approved', count: summary.approved },
    { key: 'snoozed', count: summary.snoozed },
    { key: 'dismissed', count: summary.dismissed },
  ].filter((segment) => segment.count > 0);

  const range =
    summary.first_action_at && summary.last_action_at
      ? `${formatHourMinute(summary.first_action_at, timezone)} → ${formatHourMinute(
          summary.last_action_at,
          timezone,
        )}`
      : '';

  // `elapsed_seconds` is null below two actions — there is no span between one
  // event and itself. Fall back to when the single action happened.
  const elapsed =
    summary.elapsed_seconds !== null
      ? formatDuration(summary.elapsed_seconds)
      : summary.first_action_at
        ? formatHourMinute(summary.first_action_at, timezone)
        : '—';
  const elapsedLabel = summary.elapsed_seconds !== null ? 'start to finish' : 'actioned at';

  return (
    <section className="done-hero" aria-labelledby="done-hero-headline">
      <p className="done-hero__eyebrow">Queue cleared</p>

      <h2 className="done-hero__headline" id="done-hero-headline">
        Nothing left to triage.
      </h2>
      <p className="done-hero__subline">
        You worked through every lead the planner surfaced today.
      </p>

      <div
        className="done-hero__bar"
        role="img"
        aria-label={`${summary.approved} approved, ${summary.snoozed} snoozed, ${summary.dismissed} dismissed`}
      >
        {segments.map((segment) => (
          <span
            key={segment.key}
            className={`done-hero__seg done-hero__seg--${segment.key}`}
            // Proportions are data, so they cannot live in the stylesheet. The
            // colour still does: each segment is currentColor at a fixed
            // opacity, so the whole bar is one inverted lightness ramp.
            style={{ flexGrow: segment.count }}
          />
        ))}
      </div>

      <div className="done-hero__stats">
        <div className="done-hero__stat">
          <span className="done-hero__value">{summary.total}</span>
          <span className="done-hero__label">triaged</span>
          <span className="done-hero__sub">
            {summary.approved} approved · {summary.snoozed} snoozed ·{' '}
            {summary.dismissed} dismissed
          </span>
        </div>

        <div className="done-hero__stat">
          <span className="done-hero__value">
            {formatUsdCompact(summary.pipeline_value_usd)}
          </span>
          <span className="done-hero__label">pipeline touched</span>
          {/* §5.2 sums estimated_book_size_usd over approved items only.
              Saying so is the difference between a real number and a boast. */}
          <span className="done-hero__sub">approved book size</span>
        </div>

        <div className="done-hero__stat">
          <span className="done-hero__value">{elapsed}</span>
          <span className="done-hero__label">{elapsedLabel}</span>
          <span className="done-hero__sub">{range}</span>
        </div>
      </div>

      <p className="done-hero__footer">
        <span>{formatDayLabel(data.date)}</span>
        <span className="done-hero__zone">{timezone}</span>
        <span className="done-hero__note">
          Anything you actioned in the last few minutes is still undoable below.
        </span>
      </p>
    </section>
  );
}
