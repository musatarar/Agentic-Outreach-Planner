import { getJson, postJson } from './client';
import type {
  AuthConsumeInput,
  AuthConsumeResult,
  AuthMe,
  AuthRequestLinkInput,
  AuthRequestLinkResult,
  DismissInput,
  EditCopyInput,
  LeadRecord,
  OutreachAction,
  Paginated,
  ProposedAction,
  ReviewItem,
  VerificationReport,
  VerifyCopyInput,
} from './types';

// ===== leads =======================================================

/** Every lead in the book, full records (`LeadSerializer`). */
export const fetchLeads = () => getJson<LeadRecord[]>('/api/leads/');

/** Plan the whole book. */
export const runOutreachPlan = () => postJson<OutreachAction[]>('/api/outreach/run/', {});

/** Plan one client. 409 when there is nothing new to recommend. */
export const composeForLead = (leadId: string) =>
  postJson<OutreachAction>(`/api/leads/${leadId}/compose/`, {});

// ===== the actions the engine chose ================================

/** What the engine decided for this user's leads, paginated. */
export const fetchProposals = (page?: number) =>
  getJson<Paginated<ProposedAction>>(page ? `/api/actions/?page=${page}` : '/api/actions/');

/** Draft the copy for ONE proposal. 409 when it is already drafted or dismissed. */
export const generateFromProposal = (id: number) =>
  postJson<ReviewItem>(`/api/actions/${id}/generate/`, {});

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

// ===== magic-link auth =============================================

export const fetchAuthMe = () => getJson<AuthMe>('/api/auth/me/');

export const requestLoginLink = (body: AuthRequestLinkInput) =>
  postJson<AuthRequestLinkResult>('/api/auth/request-link/', body);

export const consumeLoginToken = (body: AuthConsumeInput) =>
  postJson<AuthConsumeResult>('/api/auth/consume/', body);

export const logout = () => postJson<void>('/api/auth/logout/', {});
