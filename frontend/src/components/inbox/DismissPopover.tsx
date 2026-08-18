import type { DismissReason } from '../../api/types';
import { MenuList, Popover, type MenuOption } from './Popover';

const OPTIONS: MenuOption<Exclude<DismissReason, ''> | 'none'>[] = [
  { value: 'not_a_fit', label: 'Not a fit' },
  { value: 'bad_timing', label: 'Bad timing' },
  { value: 'wrong_contact', label: 'Wrong contact' },
  { value: 'already_handled', label: 'Already handled' },
  { value: 'copy_unusable', label: 'Copy unusable' },
  { value: 'other', label: 'Other' },
  { value: 'none', label: 'Dismiss without a reason' },
];

export interface DismissPopoverProps {
  onDismiss: (reason: DismissReason) => void;
  onClose: () => void;
}

/**
 * Why this lead is going away. The reason is optional. Dismissing suppresses
 * the lead's dedupe key server-side, so it does not return on the next run.
 */
export function DismissPopover({ onDismiss, onClose }: DismissPopoverProps) {
  return (
    <Popover label="Dismiss this lead" onClose={onClose}>
      <p className="popover__title">Dismiss because</p>
      <MenuList
        options={OPTIONS}
        onSelect={(value) => onDismiss(value === 'none' ? '' : value)}
      />
    </Popover>
  );
}
