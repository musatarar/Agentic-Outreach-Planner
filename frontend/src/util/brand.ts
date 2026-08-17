/**
 * The one place the brand name is spelled. Must match
 * project/app/templates/app/spa_base.html exactly, or the tab title visibly
 * rewrites itself when React mounts.
 */
export const BRAND_NAME = 'Locked In';

/** `documentTitle('Settings')` -> `'Settings · Locked In'`. */
export function documentTitle(pageTitle: string): string {
  return `${pageTitle} · ${BRAND_NAME}`;
}
