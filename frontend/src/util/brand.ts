/**
 * The one place the brand name is spelled (MUS-44).
 *
 * The suffix has to match project/app/templates/app/spa_base.html exactly, or
 * the tab title visibly rewrites itself the moment React mounts.
 */
export const BRAND_NAME = 'Locked In';

/** `documentTitle('Settings')` -> `'Settings · Locked In'`. */
export function documentTitle(pageTitle: string): string {
  return `${pageTitle} · ${BRAND_NAME}`;
}
