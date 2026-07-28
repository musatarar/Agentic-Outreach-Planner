import { getJson, postJson, putJson } from './client';
import type {
  DismissInput,
  DoneResponse,
  EditCopyInput,
  LLMCatalog,
  LLMConfig,
  LLMConfigInput,
  LLMTestResult,
  OutreachAction,
  QueueItem,
  QueueResponse,
  ReviewDecision,
  ReviewDecisionInput,
  ReviewQueue,
  SnoozeInput,
  VerificationReport,
  VerifyCopyInput,
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

// ===== MUS-39 / MUS-40 / MUS-41: triage queue ======================

export const fetchQueue = () => getJson<QueueResponse>('/api/queue/');

export const fetchQueueItem = (id: number) =>
  getJson<QueueItem>(`/api/queue/${id}/`);

export const fetchDone = () => getJson<DoneResponse>('/api/queue/done/');

export const editQueueCopy = (id: number, body: EditCopyInput) =>
  postJson<QueueItem>(`/api/queue/${id}/edit/`, body);

export const verifyQueueCopy = (id: number, body: VerifyCopyInput) =>
  postJson<VerificationReport>(`/api/queue/${id}/verify/`, body);

export const approveQueueItem = (id: number) =>
  postJson<QueueItem>(`/api/queue/${id}/approve/`, {});

export const snoozeQueueItem = (id: number, body: SnoozeInput) =>
  postJson<QueueItem>(`/api/queue/${id}/snooze/`, body);

export const dismissQueueItem = (id: number, body: DismissInput) =>
  postJson<QueueItem>(`/api/queue/${id}/dismiss/`, body);

export const undoQueueItem = (id: number) =>
  postJson<QueueItem>(`/api/queue/${id}/undo/`, {});
