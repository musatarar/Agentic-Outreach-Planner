import { getJson, postJson, putJson } from './client';
import type {
  AuthConsumeInput,
  AuthConsumeResult,
  AuthMe,
  AuthRequestLinkInput,
  AuthRequestLinkResult,
  DismissInput,
  EditCopyInput,
  LeadRecord,
  LLMCatalog,
  LLMConfig,
  LLMConfigInput,
  LLMTestResult,
  OutreachAction,
  Paginated,
  ReviewItem,
  VerificationReport,
  VerifyCopyInput,
} from './types';

// ===== leads =======================================================

/** Every lead in the book, full records (MUS-68's `LeadSerializer`). */
export const fetchLeads = () => getJson<LeadRecord[]>('/api/leads/');

/** Plan the whole book. */
export const runOutreachPlan = () => postJson<OutreachAction[]>('/api/outreach/run/', {});

/** Plan one client (MUS-68). 409 when there is nothing new to recommend. */
export const composeForLead = (leadId: string) =>
  postJson<OutreachAction>(`/api/leads/${leadId}/compose/`, {});

// ===== review inbox ================================================

/** The inbox: the latest action per lead, paginated. */
export const fetchOutreach = (page?: number) =>
  getJson<Paginated<ReviewItem>>(page ? `/api/outreach/?page=${page}` : '/api/outreach/');

export const editCopy = (id: number, body: EditCopyInput) =>
  postJson<ReviewItem>(`/api/outreach/${id}/edit/`, body);

/** Dry run: only `edit` persists. */
export const verifyCopy = (id: number, body: VerifyCopyInput) =>
  postJson<VerificationReport>(`/api/outreach/${id}/verify/`, body);

export const approveAction = (id: number) =>
  postJson<ReviewItem>(`/api/outreach/${id}/approve/`, {});

export const dismissAction = (id: number, body: DismissInput) =>
  postJson<ReviewItem>(`/api/outreach/${id}/dismiss/`, body);

/** Back to pending; a reopened dismissal stops suppressing the lead. */
export const reopenAction = (id: number) =>
  postJson<ReviewItem>(`/api/outreach/${id}/reopen/`, {});

// ===== LLM configuration ===========================================

export const fetchLLMCatalog = () => getJson<LLMCatalog>('/api/llm/catalog/');

export const fetchLLMConfig = () => getJson<LLMConfig>('/api/llm/config/');

export const saveLLMConfig = (config: LLMConfigInput) =>
  putJson<LLMConfig>('/api/llm/config/', config);

export const testLLMConfig = (config: LLMConfigInput) =>
  postJson<LLMTestResult>('/api/llm/config/test/', config);

// ===== magic-link auth =============================================

export const fetchAuthMe = () => getJson<AuthMe>('/api/auth/me/');

export const requestLoginLink = (body: AuthRequestLinkInput) =>
  postJson<AuthRequestLinkResult>('/api/auth/request-link/', body);

export const consumeLoginToken = (body: AuthConsumeInput) =>
  postJson<AuthConsumeResult>('/api/auth/consume/', body);

export const logout = () => postJson<void>('/api/auth/logout/', {});
