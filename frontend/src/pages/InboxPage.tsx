import { PageHeader } from '../components/PageHeader';

/**
 * Placeholder. MUS-38 lands it only so `main.tsx` and `project/urls.py` can be
 * complete in one commit and no later FE branch has to touch the router.
 * MUS-40 replaces this file wholesale.
 */
export function InboxPage() {
  return (
    <>
      <PageHeader current="/inbox" title="Triage Inbox" subtitle="One lead at a time." />
      <div className="container narrow">
        <div className="empty">The triage queue lands with MUS-40.</div>
      </div>
    </>
  );
}
