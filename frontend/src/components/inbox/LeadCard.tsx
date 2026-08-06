import type { ReactNode } from 'react';
import { Badge, Card } from '../ui';
import type { QueueItem, VerificationReport } from '../../api/types';
import { RuleTrace } from './RuleTrace';
import { VerifiedDraft } from './VerifiedDraft';

const USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <span>
      {label} <span className="lead-card__fact-value">{value}</span>
    </span>
  );
}

export interface LeadCardProps {
  item: QueueItem;
  /**
   * The report backing the underlines. Normally `item.verification`; during
   * live editing it is the `/verify/` response instead.
   */
  report: VerificationReport;
  /** `QueueResponse.date`, for the stale-trace note. */
  queueDate: string;
  /** Position in the queue, for the region label — `Lead 3 of 14`. */
  position: number;
  total: number;
  /** The draft block. Swapped for the editor while editing, in place. */
  draft?: ReactNode;
  /** Click-to-edit. The keyboard path is `E` and the Edit button, not this. */
  onDraftClick?: () => void;
  /** Verification summary and actions. */
  actions?: ReactNode;
}

/**
 * One lead, focused. The card is the whole job: who this is, what the machine
 * concluded, what it wrote, and what you can do about it.
 *
 * `reason` is deliberately not rendered. The structured rule trace above the
 * draft is the same explanation with its arithmetic showing, and printing both
 * would put a prose summary next to its own evidence, inviting the reader to
 * trust the sentence over the numbers.
 */
export function LeadCard({
  item,
  report,
  queueDate,
  position,
  total,
  draft,
  onDraftClick,
  actions,
}: LeadCardProps) {
  const { lead } = item;

  return (
    <section
      className="inbox__center"
      aria-labelledby="lead-card-heading"
      // Announced on every advance, so a screen-reader user hears which lead
      // they landed on rather than silence.
      aria-label={`Lead ${position} of ${total}`}
    >
      <Card padding="lg">
        <div className="lead-card">
          <div className="lead-card__head">
            <div className="lead-card__identity">
              <h2 className="lead-card__contact" id="lead-card-heading">
                {lead.contact_name}
              </h2>
              <p className="lead-card__agency">{lead.agency_name}</p>
            </div>
            <div className="lead-card__badges">
              <Badge tone={`p${item.priority}`}>P{item.priority}</Badge>
            </div>
          </div>

          <p className="lead-card__action">{item.action_label}</p>

          {/* The record the draft's claims are checked against. Mono, because
              every figure here is something the verifier compared. */}
          <div className="lead-card__facts">
            <Fact label="id" value={lead.id} />
            <Fact label="stage" value={lead.stage} />
            <Fact label="book" value={USD.format(lead.estimated_book_size_usd)} />
            <Fact label="quotes" value={`${lead.quotes_created}/${lead.quotes_submitted}`} />
            <Fact label="closed" value={String(lead.deals_closed)} />
            <Fact label="producers" value={String(lead.num_producers)} />
            <Fact label="last login" value={lead.last_login_date ?? '—'} />
          </div>

          <RuleTrace trace={item.rule_trace} queueDate={queueDate} />

          <div className="inbox-section">
            <div className="inbox-section__label">Draft</div>
            {draft ?? (
              // A mouse affordance only. Giving this a button role would make a
              // screen reader announce the entire email as one control label;
              // `E` and the Edit button are the accessible ways in.
              <div className="draft-open" onClick={onDraftClick}>
                <VerifiedDraft report={report} />
              </div>
            )}
          </div>

          {actions}
        </div>
      </Card>
    </section>
  );
}
