import { PageHeader } from '../components/PageHeader';

/**
 * Placeholder, same reason as InboxPage. MUS-41 replaces this file wholesale.
 */
export function DonePage() {
  return (
    <>
      <PageHeader current="/done" title="Done Today" subtitle="What you cleared." />
      <div className="container narrow">
        <div className="empty">The done view lands with MUS-41.</div>
      </div>
    </>
  );
}
