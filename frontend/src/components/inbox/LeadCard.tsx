import type { ReactNode } from 'react';
import { Badge, Card } from '../ui';
import type { ReviewItem, VerificationReport } from '../../api/types';
import { VerifiedDraft } from './VerifiedDraft';

const USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

const STATUS_TONE = {
  pending: 'pending',
  approved: 'approved',
  dismissed: 'dismissed',
} as const;

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <span>
      {label} <span className="lead-card__fact-value">{value}</span>
    </span>
  );
}

export interface LeadCardProps {
  item: ReviewItem;
  /** The report backing the underlines; the `/verify/` response during live editing. */
  report: VerificationReport;
  /** The draft block. Swapped for the editor while editing, in place. */
  draft?: ReactNode;
  /** Click-to-edit. The Edit button is the accessible way in. */
  onDraftClick?: () => void;
  /** Verification summary and actions. */
  actions?: ReactNode;
}

/**
 * One lead, with the record its draft is checked against and the rule's own
 * one-line reason for picking it.
 */
export function LeadCard({ item, report, draft, onDraftClick, actions }: LeadCardProps) {
  const { lead } = item;
  const book = lead.data.estimated_book_size_usd;
  // A row with no draft is a failed or unmatched generation: `further_action`
  // is then the whole point of the card, and says whose problem it is.
  const hasDraft = item.effective_copy.trim().length > 0;

  return (
    <section className="inbox__center" aria-labelledby={`lead-card-heading-${item.id}`}>
      <Card padding="lg">
        <div className="lead-card">
          <div className="lead-card__head">
            <div className="lead-card__identity">
              <h2 className="lead-card__contact" id={`lead-card-heading-${item.id}`}>
                {lead.data.contact_name ?? lead.id}
              </h2>
              <p className="lead-card__agency">{lead.data.agency_name ?? '—'}</p>
            </div>
            <div className="lead-card__badges">
              <Badge tone={`p${item.priority}`}>P{item.priority}</Badge>
              <Badge tone={STATUS_TONE[item.status]}>{item.status}</Badge>
            </div>
          </div>

          <p className="lead-card__action">{item.action_label}</p>

          {/* The rule's plain-text why: what put this lead in the inbox. */}
          <div className="inbox-section">
            <div className="inbox-section__label">Why</div>
            <p className="lead-card__reason">{item.reason}</p>
          </div>

          {/* The record the draft's claims are checked against. */}
          <div className="lead-card__facts">
            <Fact label="id" value={lead.id} />
            <Fact label="stage" value={lead.data.stage ?? '—'} />
            <Fact label="book" value={book === undefined ? '—' : USD.format(book)} />
            <Fact
              label="quotes"
              value={`${lead.data.quotes_created ?? 0}/${lead.data.quotes_submitted ?? 0}`}
            />
            <Fact label="closed" value={String(lead.data.deals_closed ?? 0)} />
            <Fact label="producers" value={String(lead.data.num_producers ?? 0)} />
            <Fact label="last login" value={lead.data.last_login_date ?? '—'} />
          </div>

          <div className="inbox-section">
            <div className="inbox-section__label">Draft</div>
            {hasDraft ? (
              (draft ?? (
                // Mouse affordance only: a button role would make a screen reader
                // announce the entire email as one control label; Edit is the
                // accessible way in.
                <div className="draft-open" onClick={onDraftClick}>
                  <VerifiedDraft report={report} />
                </div>
              ))
            ) : (
              <p className="lead-card__no-draft">
                No draft was generated for this lead. {item.further_action}
              </p>
            )}
          </div>

          {actions}
        </div>
      </Card>
    </section>
  );
}
