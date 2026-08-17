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
 * A labelled text field; `label` and `id` are required so an unlabelled input
 * cannot be built by accident. `error` is wired through aria-invalid and
 * aria-describedby so it is announced, not just coloured.
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
        // Opt-in only; never default this to true.
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
