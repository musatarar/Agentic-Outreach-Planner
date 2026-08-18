import type { DismissReason, QueueItem, QueueStatus, SnoozeTrigger } from '../../api/types';
import { formatDate, formatDateTime } from './format';

/** The chip on the left of a row. `pending` never reaches /done, but the union does. */
export const STATUS_LABELS: Record<QueueStatus, string> = {
  pending: 'Pending',
  approved: 'Approved',
  snoozed: 'Snoozed',
  dismissed: 'Dismissed',
};

const DISMISS_REASON_LABELS: Record<DismissReason, string> = {
  not_a_fit: 'not a fit',
  bad_timing: 'bad timing',
  wrong_contact: 'wrong contact',
  already_handled: 'already handled',
  copy_unusable: 'copy unusable',
  other: 'other',
  '': '',
};

const SNOOZE_TRIGGER_LABELS: Record<SnoozeTrigger, string> = {
  tomorrow: 'Tomorrow',
  in_3_days: 'In 3 days',
  next_week: 'Next week',
  custom: 'Custom',
  on_activity: 'On activity',
};

export function dismissReasonLabel(reason: DismissReason): string {
  return DISMISS_REASON_LABELS[reason] ?? reason;
}

export function snoozeTriggerLabel(trigger: SnoozeTrigger | ''): string {
  return trigger === '' ? '' : (SNOOZE_TRIGGER_LABELS[trigger] ?? trigger);
}

/**
 * The "what happened" line for a /done row. Switches on `status` and reads
 * `snooze.*` only inside the `snoozed` branch: the server clears snooze fields
 * on `undo` alone, so an approved-after-snooze item still carries a stale
 * `snooze.until`. `on_activity`'s `until` is the 14-day backstop, not a date.
 */
export function describeOutcome(item: QueueItem, timeZone: string): string {
  switch (item.status) {
    case 'approved':
      return item.is_edited ? 'Approved with your edits' : 'Approved as drafted';

    case 'snoozed': {
      if (item.snooze.trigger === 'on_activity') {
        const backstop = item.snooze.until
          ? ` · returns anyway on ${formatDate(item.snooze.until, timeZone)}`
          : '';
        return `Waiting for ${item.lead.contact_name} to do something${backstop}`;
      }
      if (item.snooze.until) {
        return `Returns ${formatDateTime(item.snooze.until, timeZone)}`;
      }
      const trigger = snoozeTriggerLabel(item.snooze.trigger);
      return trigger ? `Snoozed · ${trigger.toLowerCase()}` : 'Snoozed';
    }

    case 'dismissed': {
      const reason = dismissReasonLabel(item.dismiss_reason);
      return reason ? `Dismissed — ${reason}` : 'Dismissed — no reason given';
    }

    default:
      return 'Back in your inbox';
  }
}
