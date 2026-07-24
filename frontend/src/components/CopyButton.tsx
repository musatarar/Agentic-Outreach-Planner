import { useEffect, useRef, useState } from 'react';

/** Copy-to-clipboard button that flashes "Copied!" for two seconds. */
export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  async function handleClick() {
    try {
      await navigator.clipboard.writeText(text);
      setFailed(null);
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 2000);
    } catch (error) {
      setFailed(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <>
      <button
        type="button"
        className={`copy-button${copied ? ' copied' : ''}`}
        onClick={handleClick}
      >
        {copied ? 'Copied!' : 'Copy'}
      </button>
      {failed && <div className="card-error">Failed to copy: {failed}</div>}
    </>
  );
}
