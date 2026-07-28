import { getJson, postJson, putJson } from './client';
import type {
  AuthConsumeInput,
  AuthConsumeResult,
  AuthMe,
  AuthRequestLinkInput,
  AuthRequestLinkResult,
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

// ===== MUS-37 / MUS-38: magic-link auth ============================

export const fetchAuthMe = () => getJson<AuthMe>('/api/auth/me/');

export const requestLoginLink = (body: AuthRequestLinkInput) =>
  postJson<AuthRequestLinkResult>('/api/auth/request-link/', body);

export const consumeLoginToken = (body: AuthConsumeInput) =>
  postJson<AuthConsumeResult>('/api/auth/consume/', body);

export const logout = () => postJson<void>('/api/auth/logout/', {});
