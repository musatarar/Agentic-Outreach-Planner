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
 * `summary.queue_cleared === true`. A server flag, never inferred — the FE
 * cannot know `remaining`, and deriving it from `items.length` would light
 * this screen up while leads were still waiting.
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

  // `elapsed_seconds` is null below two actions; fall back to the timestamp.
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
            // Proportions are data, so they cannot live in the stylesheet.
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
          {/* The server sums estimated_book_size_usd over approved items only. */}
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
