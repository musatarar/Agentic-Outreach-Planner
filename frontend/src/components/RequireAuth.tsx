import type { ReactNode } from 'react';

/**
 * Route guard. Structural placeholder so `main.tsx` can land complete in one
 * commit; the real guard (session check, redirect to /signin preserving the
 * intended destination, global 401 handler) lands in
 * `musansht/mus-38-c-consume-and-guard`. Do not ship the ticket branch without
 * that PR merged — this version renders its children unconditionally.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
