export interface KeyHintProps {
  keys: string[];
}

/** The mono keyboard chips — `J K move`, `A approve`. */
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
