/**
 * CONTRACT §9.4 — the 401 handler must fire on a *real* 401, not a mocked one.
 *
 * The failure this exists to catch: DRF answers 403, not 401, for an anonymous
 * request when the first authenticator's `authenticate_header()` returns None,
 * which is exactly what plain SessionAuthentication does. A suite that stubs
 * `fetch` with a hand-rolled `{status: 401}` object passes whatever the server
 * actually does, and the whole route guard is dead code nobody notices.
 *
 * So these tests stand up a real `node:http` server and let the platform's real
 * `fetch` talk to it over a real socket. Every Response here — status line,
 * headers, JSON body — is produced by an actual HTTP exchange.
 *
 * The one thing stubbed is *URL base resolution*: `client.ts` calls
 * `fetch('/api/queue/')` because in a browser the origin is implied, and Node's
 * fetch rejects relative URLs. `withServer` therefore resolves the path against
 * the test server's origin and delegates to the real fetch. That is the
 * browser's job, not the client's, and it leaves the response untouched.
 *
 * Run with `npm test` (Node's built-in runner + type stripping; no new deps).
 */
import assert from 'node:assert/strict';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, test } from 'node:test';

import { ApiError, getJson, postJson, setUnauthorizedHandler } from '../src/api/client.ts';

interface Reply {
  status: number;
  /** Omitted for 204, which must not carry one. */
  body?: unknown;
}

const realFetch = globalThis.fetch;

// client.ts reads document.cookie for the CSRF header. The DOM is not what is
// under test here; an empty cookie jar just means no X-CSRFToken is sent.
(globalThis as { document?: { cookie: string } }).document ??= { cookie: '' };

/**
 * Serve `reply` from a real socket for the duration of `run`, with relative
 * request paths resolved against it.
 */
async function withServer(reply: Reply, run: () => Promise<void>): Promise<void> {
  const server = http.createServer((_req, response) => {
    if (reply.body === undefined) {
      response.writeHead(reply.status);
      response.end();
      return;
    }
    response.writeHead(reply.status, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify(reply.body));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;
  const origin = `http://127.0.0.1:${port}`;

  globalThis.fetch = ((input: string, init?: RequestInit) =>
    realFetch(new URL(input, origin), init)) as typeof fetch;

  try {
    await run();
  } finally {
    globalThis.fetch = realFetch;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

afterEach(() => {
  setUnauthorizedHandler(null);
});

test('a real 401 from a non-auth endpoint calls the unauthorized handler', async () => {
  let calls = 0;
  setUnauthorizedHandler(() => {
    calls += 1;
  });

  await withServer(
    {
      status: 401,
      body: {
        code: 'not_authenticated',
        detail: 'Authentication credentials were not provided.',
      },
    },
    async () => {
      const error = await getJson('/api/queue/').then(
        () => null,
        (caught: unknown) => caught,
      );

      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 401);
      // The FE branches on `code`, so it has to survive toError intact.
      assert.equal(error.code, 'not_authenticated');
      assert.equal(error.message, 'Authentication credentials were not provided.');
    },
  );

  assert.equal(calls, 1);
});

test('a real 401 from /api/auth/me/ does NOT call the handler', async () => {
  // GET /api/auth/me/ answers 401 by design so the guard can ask "am I signed
  // in?". Routing that through the redirect handler would loop.
  let calls = 0;
  setUnauthorizedHandler(() => {
    calls += 1;
  });

  await withServer(
    { status: 401, body: { code: 'not_authenticated', detail: 'nope' } },
    async () => {
      await getJson('/api/auth/me/').catch(() => undefined);
    },
  );

  assert.equal(calls, 0);
});

test('a real 403 does NOT call the handler', async () => {
  // This is the §9.4 canary. If the backend regresses to plain
  // SessionAuthentication, anonymous requests arrive as 403 and this handler
  // stays silent — which is the bug, and the first test above is what fails.
  let calls = 0;
  setUnauthorizedHandler(() => {
    calls += 1;
  });

  await withServer(
    { status: 403, body: { code: 'csrf_failed', detail: 'CSRF Failed' } },
    async () => {
      const error = await getJson('/api/queue/').then(
        () => null,
        (caught: unknown) => caught,
      );
      assert.ok(error instanceof ApiError);
      assert.equal(error.code, 'csrf_failed');
    },
  );

  assert.equal(calls, 0);
});

test('a real 401 on a POST calls the handler too', async () => {
  let calls = 0;
  setUnauthorizedHandler(() => {
    calls += 1;
  });

  await withServer(
    { status: 401, body: { code: 'not_authenticated', detail: 'nope' } },
    async () => {
      await postJson('/api/queue/1/approve/', {}).catch(() => undefined);
    },
  );

  assert.equal(calls, 1);
});

test('a 204 POST resolves instead of throwing on an empty body', async () => {
  // POST /api/auth/logout/ answers 204 no-content; response.json() would throw.
  await withServer({ status: 204 }, async () => {
    const result = await postJson<void>('/api/auth/logout/', {});
    assert.equal(result, undefined);
  });
});
