/**
 * The intended-destination round trip: the guard stores where you were headed,
 * ConsumePage reads it back after the link lands, and nothing else survives.
 *
 * Worth testing directly because the failure modes are silent. A destination
 * that is never cleared replays an old page on the next sign-in, and one that
 * is not validated is an open redirect: `//evil.example` is a protocol-relative
 * URL that `startsWith('/')` alone happily accepts.
 */
import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

// A real Storage-shaped object; the browser's own is not what is under test.
const store = new Map<string, string>();
(globalThis as { sessionStorage?: unknown }).sessionStorage = {
  getItem: (key: string) => store.get(key) ?? null,
  setItem: (key: string, value: string) => void store.set(key, value),
  removeItem: (key: string) => void store.delete(key),
};

const { rememberDestination, takeDestination } = await import('../src/hooks/authDestination.ts');

beforeEach(() => {
  store.clear();
});

test('a remembered path is handed back once, then forgotten', () => {
  rememberDestination('/inbox?lead=12');
  assert.equal(takeDestination(), '/inbox?lead=12');
  // Second read must not replay it — a later sign-in belongs on the default.
  assert.equal(takeDestination(), '/inbox');
});

test('the default destination is /inbox', () => {
  assert.equal(takeDestination(), '/inbox');
});

test('/signin is never remembered as a destination', () => {
  // Otherwise a 401 raised while already on the sign-in screen would loop.
  rememberDestination('/signin');
  assert.equal(takeDestination(), '/inbox');
});

test('off-origin destinations are refused', () => {
  for (const hostile of [
    'https://evil.example/inbox',
    '//evil.example/inbox',
    'javascript:alert(1)',
    'inbox',
  ]) {
    store.clear();
    rememberDestination(hostile);
    assert.equal(takeDestination(), '/inbox', `should have refused ${hostile}`);
  }
});

test('a hostile value already in storage is refused on read', () => {
  // Storage is attacker-writable from any script on the origin, so the check
  // has to hold on the way out as well as on the way in.
  store.set('auth:destination', '//evil.example');
  assert.equal(takeDestination(), '/inbox');
});

test('no token or credential is written to storage', () => {
  rememberDestination('/done');
  assert.deepEqual([...store.keys()], ['auth:destination']);
});
