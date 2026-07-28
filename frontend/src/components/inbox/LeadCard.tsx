import { Badge, Card } from '../ui';
import type { QueueItem } from '../../api/types';
import { subjectLabelLength } from './draftText';

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

/**
 * The draft, in the serif voice face.
 *
 * Plain text for now: mini PR 40-c replaces the body with the span renderer so
 * verified claims carry their underlines. The typography is settled here so
 * that swap changes nothing about the layout.
 */
function DraftBody({ copy }: { copy: string }) {
  const labelLength = subjectLabelLength(copy);
  if (labelLength === 0) return <div className="draft">{copy}</div>;
  return (
    <div className="draft">
      <span className="draft__subject-label">{copy.slice(0, labelLength)}</span>
      {copy.slice(labelLength)}
    </div>
  );
}

export interface LeadCardProps {
  item: QueueItem;
  /** Position in the queue, for the region label — `Lead 3 of 14`. */
  position: number;
  total: number;
}

/**
 * One lead, focused. The card is the whole job: who this is, what the machine
 * concluded, what it wrote, and what you can do about it.
 *
 * `reason` is deliberately not rendered. The structured rule trace that mini PR
 * 40-c adds below the header is the same explanation with its arithmetic
 * showing, and printing both would put a prose summary and its own evidence
 * side by side, inviting the reader to trust the sentence over the numbers.
 */
export function LeadCard({ item, position, total }: LeadCardProps) {
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

          <div className="inbox-section">
            <div className="inbox-section__label">Draft</div>
            <DraftBody copy={item.effective_copy} />
          </div>
        </div>
      </Card>
    </section>
  );
}
