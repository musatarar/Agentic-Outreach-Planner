/**
 * Reading `ApiErrorBody.code` off a thrown error (CONTRACT §5.3).
 *
 * `ApiError` grows a `code` field in MUS-38's pinned `client.ts` diff
 * (CONTRACT §6.3), and MUS-38 is this ticket's sole owner of that file — so
 * MUS-41 cannot add the field itself and cannot wait for it either. Reading it
 * structurally compiles against the current `client.ts` and starts returning
 * real codes the moment MUS-38 merges, with no edit here.
 */
export function apiErrorCode(error: unknown): string {
  if (error !== null && typeof error === 'object' && 'code' in error) {
    const code = (error as { code?: unknown }).code;
    if (typeof code === 'string') return code;
  }
  return '';
}

/**
 * Did this failure mean "the undo window has closed"?
 *
 * `undo_window_expired` and `invalid_transition` are both 409s, so the status
 * alone cannot tell them apart. Once MUS-38 has landed `code`, the first branch
 * decides it. Until then the fallback reads the `detail` sentence the contract
 * pins for this one code — deliberately narrow, and dead code the day `code`
 * starts arriving.
 */
export function isUndoWindowExpired(error: unknown): boolean {
  const code = apiErrorCode(error);
  if (code) return code === 'undo_window_expired';
  return error instanceof Error && /undo window/i.test(error.message);
}
