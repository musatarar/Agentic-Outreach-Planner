/**
 * Writing the approved draft to the clipboard.
 *
 * The user's literal next action is always paste-into-Gmail, so an approve that
 * does not hand them the text is a broken loop — the keystroke would look like
 * it worked and leave them to select the email by hand.
 *
 * `CopyButton` covers the ordinary click-to-copy affordance and is reused
 * as-is. This exists because the approve path cannot: it has to write the
 * clipboard **synchronously inside the keydown handler**, before any `await`,
 * or Safari and Firefox drop the user-gesture that authorises the write.
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

  // The pre-async-clipboard path. Still the only thing that works over plain
  // HTTP, which is exactly how this app is demoed on a LAN address.
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
