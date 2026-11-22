import type {
  Approval,
  ApprovalStatus,
  Candidate,
  CandidateDetail,
  Disposition,
  EventRow,
  JobCreateIn,
  JobDetail,
  JobPatchIn,
  JobSummary,
  ShortlistEntry,
} from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
const DEMO_TOKEN = process.env.NEXT_PUBLIC_DEMO_TOKEN;

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function req<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) };
  if (init?.json !== undefined) headers["Content-Type"] = "application/json";
  if (DEMO_TOKEN) headers["X-Demo-Token"] = DEMO_TOKEN;
  const res = await fetch(`${API_URL}/api${path}`, {
    ...init,
    headers,
    body: init?.json !== undefined ? JSON.stringify(init.json) : init?.body,
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {}
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  listJobs: () => req<JobSummary[]>("/jobs"),
  getJob: (id: string) => req<JobDetail>(`/jobs/${id}`),
  createJob: (body: JobCreateIn) => req<JobDetail>("/jobs", { method: "POST", json: body }),
  patchJob: (id: string, body: JobPatchIn) => req<JobDetail>(`/jobs/${id}`, { method: "PATCH", json: body }),
  startJob: (id: string) => req<JobDetail>(`/jobs/${id}/start`, { method: "POST" }),
  pauseJob: (id: string) => req<JobDetail>(`/jobs/${id}/pause`, { method: "POST" }),
  resumeJob: (id: string) => req<JobDetail>(`/jobs/${id}/resume`, { method: "POST" }),
  shortlist: (id: string) => req<ShortlistEntry[]>(`/jobs/${id}/shortlist`),

  listCandidates: (jobId: string) => req<Candidate[]>(`/jobs/${jobId}/candidates`),
  getCandidate: (id: string) => req<CandidateDetail>(`/candidates/${id}`),
  resumeCandidate: (id: string) => req<CandidateDetail>(`/candidates/${id}/resume`, { method: "POST" }),
  skipCandidate: (id: string) => req<CandidateDetail>(`/candidates/${id}/skip`, { method: "POST" }),
  setDisposition: (id: string, demoDisposition: Disposition | null) =>
    req<CandidateDetail>(`/candidates/${id}`, { method: "PATCH", json: { demoDisposition } }),

  listApprovals: (jobId: string, status?: ApprovalStatus) =>
    req<Approval[]>(`/jobs/${jobId}/approvals${status ? `?status=${status}` : ""}`),
  approve: (id: string, payload?: Record<string, unknown>) =>
    req<Approval>(`/approvals/${id}/approve`, { method: "POST", json: payload ? { payload } : {} }),
  reject: (id: string, feedback: string) =>
    req<Approval>(`/approvals/${id}/reject`, { method: "POST", json: { feedback } }),

  events: (jobId: string, after = 0) => req<EventRow[]>(`/jobs/${jobId}/events?after=${after}`),
};

export function streamUrl(jobId: string, after: number) {
  const q = new URLSearchParams({ after: String(after) });
  if (DEMO_TOKEN) q.set("token", DEMO_TOKEN);
  return `${API_URL}/api/jobs/${jobId}/stream?${q}`;
}
