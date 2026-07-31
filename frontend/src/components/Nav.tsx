import { Link } from 'react-router-dom';
import { SignOutButton } from './SignOutButton';

// Mirrors the route table in main.tsx, which in turn mirrors project/urls.py.
// The trailing-slash asymmetry is deliberate and load-bearing: /inbox and /done
// have none, the four legacy routes do, and a mismatch here 404s on hard
// refresh even though client-side navigation looks fine.
const LINKS = [
  { to: '/inbox', label: 'Inbox' },
  { to: '/done', label: 'Done' },
  { to: '/', label: 'Planner' },
  { to: '/reports/', label: 'Reports' },
  { to: '/next-actions/', label: 'BD Dashboard' },
  { to: '/settings/', label: 'Settings' },
];

/**
 * Shared header. The current page renders as bold text rather than a link.
 *
 * Nav only ever renders inside RequireAuth, so the sign-out control lives here
 * rather than in PageHeader (MUS-36's file): every surface that shows this nav
 * is by definition a signed-in one, and the logged-out screens use AuthShell
 * instead.
 */
export function Nav({ current }: { current: string }) {
  return (
    <nav>
      {LINKS.map((link, index) => (
        <span key={link.to}>
          {index > 0 && <span className="sep">|</span>}
          {link.to === current ? (
            <strong>{link.label}</strong>
          ) : (
            <Link to={link.to}>{link.label}</Link>
          )}
        </span>
      ))}
      <span className="sep">|</span>
      <SignOutButton />
    </nav>
  );
}
