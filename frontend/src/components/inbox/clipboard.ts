/**
 * Writes the approved draft to the clipboard. Separate from `CopyButton`
 * because the approve path must write synchronously inside the keydown
 * handler, before any `await`, or Safari and Firefox drop the user gesture.
 */
export async function writeToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Denied permission, or a non-secure context. Fall through.
  }

  // Legacy path — still the only one that works over plain HTTP.
  try {
    const staging = document.createElement('textarea');
    staging.value = text;
    staging.setAttribute('readonly', '');
    staging.style.position = 'fixed';
    staging.style.opacity = '0';
    document.body.appendChild(staging);
    staging.select();
    const copied = document.execCommand('copy');
    document.body.removeChild(staging);
    return copied;
  } catch {
    return false;
  }
}
