export interface KeyHintProps {
  keys: string[];
}

/**
 * The mono keyboard chips — `J K move`, `A approve`. Reused across the inbox
 * and the empty states, which is the point: the shortcuts have to be visible
 * everywhere or nobody learns them.
 *
 * Keys are machine input, so they are always --font-mono, never sans.
 */
export function KeyHint({ keys }: KeyHintProps) {
  return (
    <span className="ui-keyhint">
      {keys.map((key) => (
        <kbd key={key} className="ui-keyhint__key">
          {key}
        </kbd>
      ))}
    </span>
  );
}
