/** Reads `ApiErrorBody.code` off a thrown error, structurally. */
export function apiErrorCode(error: unknown): string {
  if (error !== null && typeof error === 'object' && 'code' in error) {
    const code = (error as { code?: unknown }).code;
    if (typeof code === 'string') return code;
  }
  return '';
}

/**
 * Did this failure mean "the undo window has closed"? `undo_window_expired`
 * and `invalid_transition` are both 409s, so status alone cannot tell them
 * apart; the message fallback covers responses that carry no `code`.
 */
export function isUndoWindowExpired(error: unknown): boolean {
  const code = apiErrorCode(error);
  if (code) return code === 'undo_window_expired';
  return error instanceof Error && /undo window/i.test(error.message);
}
