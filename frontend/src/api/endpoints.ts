import { getJson, postJson, putJson } from './client';
import type {
  LLMCatalog,
  LLMConfig,
  LLMConfigInput,
  LLMTestResult,
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

export const fetchLLMCatalog = () => getJson<LLMCatalog>('/api/llm/catalog/');

export const fetchLLMConfig = () => getJson<LLMConfig>('/api/llm/config/');

export const saveLLMConfig = (config: LLMConfigInput) =>
  putJson<LLMConfig>('/api/llm/config/', config);

export const testLLMConfig = (config: LLMConfigInput) =>
  postJson<LLMTestResult>('/api/llm/config/test/', config);
