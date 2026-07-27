import { Link } from 'react-router-dom';

const LINKS = [
  { to: '/', label: 'Planner' },
  { to: '/reports/', label: 'Reports' },
  { to: '/next-actions/', label: 'BD Dashboard' },
  { to: '/settings/', label: 'Settings' },
];

/** Shared header. The current page renders as bold text rather than a link. */
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
    </nav>
  );
}
