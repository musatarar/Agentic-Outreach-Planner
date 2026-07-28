import { useState } from 'react';
import { Button, Input } from '../ui';
import type { SnoozeInput, SnoozeTrigger } from '../../api/types';
import { MenuList, Popover, type MenuOption } from './Popover';

const OPTIONS: MenuOption<SnoozeTrigger>[] = [
  { value: 'tomorrow', label: 'Tomorrow', hint: '09:00' },
  { value: 'in_3_days', label: 'In 3 days', hint: '09:00' },
  { value: 'next_week', label: 'Next week', hint: 'Monday 09:00' },
  { value: 'custom', label: 'Pick a date…' },
  {
    value: 'on_activity',
    label: 'When they do something',
    // The option this whole picker exists for.
    hint: 'Wakes on their next login, quote or deal',
  },
];

export interface SnoozePopoverProps {
  /** `QueueResponse.date` — the floor for the date picker (§9.5). */
  queueDate: string;
  onSnooze: (input: SnoozeInput) => void;
  onClose: () => void;
}

/**
 * The snooze picker.
 *
 * Four of the five options are ordinary time arithmetic. The fifth,
 * `on_activity`, is the judgement a human most wants to express — "come back
 * when they actually log in" — and can almost never express in tools like
 * this, so it is spelled out in plain language rather than hidden behind a
 * jargon label.
 *
 * The server converts every trigger into a concrete timestamp, including a
 * 14-day backstop on `on_activity` (§9.17), so a lead that never does anything
 * still resurfaces rather than quietly becoming a dismissal nobody chose.
 */
export function SnoozePopover({ queueDate, onSnooze, onClose }: SnoozePopoverProps) {
  const [customDate, setCustomDate] = useState('');
  const [picking, setPicking] = useState(false);

  function choose(trigger: SnoozeTrigger) {
    if (trigger === 'custom') {
      setPicking(true);
      return;
    }
    onSnooze({ trigger, until: null });
  }

  function submitCustom() {
    if (!customDate) return;
    // 09:00 UTC matches the hour the server uses for its own relative
    // triggers, so a hand-picked date lands in the same morning slot as
    // "tomorrow" rather than at midnight.
    onSnooze({ trigger: 'custom', until: `${customDate}T09:00:00Z` });
  }

  return (
    <Popover label="Snooze this lead" onClose={onClose}>
      <p className="popover__title">Snooze until</p>

      {picking ? (
        <div className="popover__form">
          {/* The Input primitive takes no `min` and may not be forked (§7.4),
              so the floor is stated rather than enforced client-side. The
              server rejects a past date with 400 `invalid_snooze` and is the
              authority either way. The date shown is the queue's, never the
              browser's clock (§9.5). */}
          <Input
            label={`Date (after ${queueDate})`}
            id="snooze-date"
            type="date"
            value={customDate}
            onChange={(event) => setCustomDate(event.target.value)}
            autoFocus
          />
          <div className="popover__form-actions">
            <Button variant="primary" size="sm" disabled={!customDate} onClick={submitCustom}>
              Snooze
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPicking(false)}>
              Back
            </Button>
          </div>
        </div>
      ) : (
        <MenuList options={OPTIONS} onSelect={choose} />
      )}
    </Popover>
  );
}
