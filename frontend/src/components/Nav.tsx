import { Link } from 'react-router-dom';
import { SignOutButton } from './SignOutButton';

// Mirrors the route table in main.tsx. The trailing-slash asymmetry is
// load-bearing: a mismatch 404s on hard refresh even though client-side
// navigation looks fine.
const LINKS = [
  { to: '/inbox', label: 'Inbox' },
  { to: '/done', label: 'Done' },
  { to: '/', label: 'Planner' },
  { to: '/reports/', label: 'Reports' },
  { to: '/next-actions/', label: 'BD Dashboard' },
  { to: '/settings/', label: 'Settings' },
];

/**
 * Shared header; the current page renders as bold text rather than a link.
 * Only ever rendered inside RequireAuth, so sign-out lives here.
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
