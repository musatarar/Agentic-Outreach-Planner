import type { ChangeEventHandler } from 'react';

export interface InputProps {
  label: string;
  id: string;
  type?: string;
  value: string;
  onChange: ChangeEventHandler<HTMLInputElement>;
  placeholder?: string;
  error?: string;
  autoFocus?: boolean;
}

/**
 * A labelled text field. `label` and `id` are required rather than optional so
 * an unlabelled input cannot be built by accident.
 *
 * `error` is wired through aria-invalid and aria-describedby, so the message is
 * announced rather than merely coloured — the red here is the priority ramp's
 * red, and colour alone must never be the only signal.
 */
export function Input({
  label,
  id,
  type = 'text',
  value,
  onChange,
  placeholder,
  error,
  autoFocus,
}: InputProps) {
  const errorId = `${id}-error`;
  return (
    <div className="ui-field">
      <label className="ui-field__label" htmlFor={id}>
        {label}
      </label>
      <input
        className={`ui-input${error ? ' ui-input--error' : ''}`}
        id={id}
        type={type}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
        // Opt-in only: sign-in and the inbox search are single-field screens,
        // where focus belongs in the field. Never default it to true.
        autoFocus={autoFocus}
      />
      {error && (
        <p className="ui-field__error" id={errorId} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
