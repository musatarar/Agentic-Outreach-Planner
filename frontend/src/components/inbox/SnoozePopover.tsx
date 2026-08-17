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
    hint: 'Wakes on their next login, quote or deal',
  },
];

export interface SnoozePopoverProps {
  /** `QueueResponse.date` — the floor for the date picker. */
  queueDate: string;
  onSnooze: (input: SnoozeInput) => void;
  onClose: () => void;
}

/**
 * The snooze picker. The server converts every trigger into a concrete
 * timestamp, including a 14-day backstop on `on_activity` so a lead that never
 * does anything still resurfaces.
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
    // 09:00 UTC matches the hour the server uses for its relative triggers.
    onSnooze({ trigger: 'custom', until: `${customDate}T09:00:00Z` });
  }

  return (
    <Popover label="Snooze this lead" onClose={onClose}>
      <p className="popover__title">Snooze until</p>

      {picking ? (
        <div className="popover__form">
          {/* The Input primitive takes no `min`, so the floor is stated
              rather than enforced; the server rejects a past date with 400
              `invalid_snooze`. The date shown is the queue's, not the
              browser's. */}
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
