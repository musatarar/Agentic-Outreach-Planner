import { Badge, Button } from '../ui';
import type { LeadRecord, ProposedAction } from '../../api/types';
import { formatDateOnly, formatStage, formatTimestamp, formatUsdCompact } from '../../util/labels';
import { canGenerate, urgencyTone } from './proposals';
import type { SortDirection, SortKey, SortState } from './leadTable';

interface Column {
  key: SortKey;
  label: string;
  /** Right-aligned, mono: figures read as machine output, per the inbox rules. */
  numeric?: boolean;
}

const COLUMNS: Column[] = [
  { key: 'agency_name', label: 'Agency' },
  { key: 'contact_name', label: 'Contact' },
  { key: 'stage', label: 'Stage' },
  { key: 'estimated_book_size_usd', label: 'Book size', numeric: true },
  { key: 'last_contacted_date', label: 'Last contacted', numeric: true },
];

/** Sortable columns, plus proposed action and status. */
const COLUMN_COUNT = COLUMNS.length + 2;

/** `aria-sort` carries a direction only on the column actually sorted. */
function ariaSort(active: boolean, direction: SortDirection) {
  if (!active) return 'none' as const;
  return direction === 'asc' ? ('ascending' as const) : ('descending' as const);
}

interface DetailProps {
  proposal: ProposedAction | undefined;
  generating: number | null;
  onGenerate: (proposal: ProposedAction) => void;
}

/**
 * The expanded row: why the engine chose this action, and the one button that
 * turns the choice into copy.
 *
 * A lead with no proposal still expands. "The engine has not chosen anything
 * for this lead" is an answer to the click, and a row that silently refuses to
 * open reads as a broken control.
 */
function LeadDetail({ proposal, generating, onGenerate }: DetailProps) {
  if (!proposal) {
    return (
      <p className="lead-detail__empty">
        No action chosen for this lead yet. The engine judges the book on its own schedule;
        a lead it cannot make a case for stays blank.
      </p>
    );
  }
  return (
    <>
      <ul className="lead-detail__reasons">
        {proposal.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      <div className="lead-detail__foot">
        <span className="lead-detail__decided">
          Weight {proposal.weight ?? '—'} · decided{' '}
          {proposal.decided_at ? formatTimestamp(proposal.decided_at) : 'unknown'}
        </span>
        {canGenerate(proposal) ? (
          <Button
            size="sm"
            loading={generating === proposal.id}
            disabled={generating !== null}
            onClick={() => onGenerate(proposal)}
          >
            Generate email
          </Button>
        ) : (
          <span className="lead-detail__drafted">Drafted — review it in the inbox</span>
        )}
      </div>
    </>
  );
}

interface Props {
  leads: LeadRecord[];
  sort: SortState;
  onSort: (key: SortKey) => void;
  /** Lead ids with an item still awaiting review — see `openLeadIds`. */
  open: Set<string>;
  /** What the engine chose, per lead — see `proposalsByLead`. */
  proposals: Map<string, ProposedAction>;
  /** The one lead whose decision is expanded, if any. */
  expanded: string | null;
  onToggle: (leadId: string) => void;
  /** The proposal a draft is being generated for, if any. */
  generating: number | null;
  onGenerate: (proposal: ProposedAction) => void;
}

/**
 * The book, as a table. Presentational only: ordering, the review flags and
 * each row's proposal arrive already resolved, so this file holds no logic
 * worth testing and the logic that matters is tested without a DOM.
 *
 * One row expands at a time. The decision panel is long enough that two open at
 * once pushes the rest of the book off the screen.
 */
export function LeadsTable({
  leads,
  sort,
  onSort,
  open,
  proposals,
  expanded,
  onToggle,
  generating,
  onGenerate,
}: Props) {
  return (
    <div className="leads-table-wrap">
      <table className="leads-table">
        <caption className="leads-table__caption">
          Leads, sorted by {sort.key.replace(/_/g, ' ')}, {sort.direction}ending. Choosing a
          lead opens the decision behind its proposed action.
        </caption>
        <thead>
          <tr>
            {COLUMNS.map((column) => {
              const active = sort.key === column.key;
              return (
                <th
                  key={column.key}
                  scope="col"
                  aria-sort={ariaSort(active, sort.direction)}
                  className={column.numeric ? 'leads-table__num' : undefined}
                >
                  <button
                    type="button"
                    className="leads-table__sort"
                    onClick={() => onSort(column.key)}
                  >
                    {column.label}
                    {/* Only the sorted column carries an arrow: an indicator on
                        every header reads as five sorted columns. */}
                    <span aria-hidden="true" className="leads-table__arrow">
                      {active ? (sort.direction === 'asc' ? '↑' : '↓') : ''}
                    </span>
                  </button>
                </th>
              );
            })}
            <th scope="col">Proposed action</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {leads.map((lead) => {
            const proposal = proposals.get(lead.id);
            const isOpen = expanded === lead.id;
            return [
              <tr key={lead.id} className={isOpen ? 'leads-table__row--open' : undefined}>
                <td>
                  {/* The row's own control rather than a click handler on the
                      <tr>: a button is what a keyboard and a screen reader can
                      both reach. */}
                  <button
                    type="button"
                    className="leads-table__expand"
                    aria-expanded={isOpen}
                    // Only while it exists: the panel is not rendered when closed.
                    aria-controls={isOpen ? `lead-detail-${lead.id}` : undefined}
                    onClick={() => onToggle(lead.id)}
                  >
                    <span aria-hidden="true" className="leads-table__chevron">
                      {isOpen ? '▾' : '▸'}
                    </span>
                    <span>
                      <span className="leads-table__agency">{lead.agency_name}</span>
                      <span className="leads-table__id">{lead.id}</span>
                    </span>
                  </button>
                </td>
                <td>
                  <span className="leads-table__contact">{lead.contact_name}</span>
                  <a className="leads-table__email" href={`mailto:${lead.contact_email}`}>
                    {lead.contact_email}
                  </a>
                </td>
                <td>{formatStage(lead.stage)}</td>
                <td className="leads-table__num">
                  {formatUsdCompact(lead.estimated_book_size_usd)}
                </td>
                <td className="leads-table__num">{formatDateOnly(lead.last_contacted_date)}</td>
                <td className="leads-table__proposal">
                  {proposal ? (
                    <Badge tone={urgencyTone(proposal.action.urgency)}>
                      {proposal.action.label}
                    </Badge>
                  ) : (
                    <span className="leads-table__idle">—</span>
                  )}
                </td>
                <td>
                  {open.has(lead.id) ? (
                    <Badge tone="pending">Awaiting review</Badge>
                  ) : (
                    <span className="leads-table__idle">—</span>
                  )}
                </td>
              </tr>,
              // Rendered only when open: an always-present hidden row doubles
              // the table's size for every book, on every sort.
              isOpen && (
                <tr key={`${lead.id}-detail`} className="leads-table__detail">
                  <td colSpan={COLUMN_COUNT} id={`lead-detail-${lead.id}`}>
                    <LeadDetail
                      proposal={proposal}
                      generating={generating}
                      onGenerate={onGenerate}
                    />
                  </td>
                </tr>
              ),
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}
