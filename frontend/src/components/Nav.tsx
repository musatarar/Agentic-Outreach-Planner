import { Link } from 'react-router-dom';
import { SignOutButton } from './SignOutButton';
import { useAuth } from '../hooks/useAuth';

// Mirrors the route table in main.tsx. The trailing-slash asymmetry is
// load-bearing: a mismatch 404s on hard refresh even though client-side
// navigation looks fine.
const LINKS = [
  { to: '/leads/', label: 'Leads' },
  { to: '/inbox', label: 'Inbox' },
];

/**
 * Shared header; the current page renders as bold text rather than a link.
 * Only ever rendered inside RequireAuth, so sign-out lives here.
 */
export function Nav({ current }: { current: string }) {
  // Which book of leads you are looking at. Absent while the session probe is
  // in flight, and for an account that belongs to no workspace.
  const { tenant } = useAuth();

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
      {tenant && (
        <>
          <span className="workspace">{tenant.name}</span>
          <span className="sep">|</span>
        </>
      )}
      <SignOutButton />
    </nav>
  );
}
