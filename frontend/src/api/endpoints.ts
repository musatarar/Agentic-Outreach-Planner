import { getJson, postJson } from './client';
import type {
  OutreachAction,
  ReviewDecision,
  ReviewDecisionInput,
  ReviewQueue,
} from './types';

export const fetchOutreach = () => getJson<OutreachAction[]>('/api/outreach/');

export const runOutreachPlan = () =>
  postJson<OutreachAction[]>('/api/outreach/run/', {});

export const fetchReports = () => getJson<OutreachAction[]>('/api/reports/');

export const fetchReviewQueue = () => getJson<ReviewQueue>('/api/review-queue/');

export const fetchReviewDecisions = () =>
  getJson<ReviewDecision[]>('/api/review-decisions/');

export const createReviewDecision = (decision: ReviewDecisionInput) =>
  postJson<ReviewDecision>('/api/review-decisions/', decision);
