"use client";

import { create } from "zustand";
import { api } from "./api";
import { subscribeEvents, type StreamStatus } from "./sse";
import type { Approval, Candidate, CandidateDetail, EventRow, JobDetail } from "./types";

export type LocalChat = {
  id: string;
  role: "user" | "assistant";
  text: string;
  createdAt: string;
};

type JobStore = {
  jobId: string | null;
  job: JobDetail | null;
  events: EventRow[];
  eventIds: Set<number>;
  candidates: Candidate[];
  approvals: Record<string, Approval>;
  details: Record<string, CandidateDetail>;
  chat: LocalChat[];
  streamStatus: StreamStatus;
  focusedApprovalId: string | null;
  candidateFilter: string | null;
  drawerCandidateId: string | null;
  error: string | null;

  connect: (jobId: string) => () => void;
  refreshJob: () => Promise<void>;
  refreshCandidates: () => Promise<void>;
  refreshApprovals: () => Promise<void>;
  refreshDetail: (candidateId: string) => Promise<CandidateDetail | null>;
  setFocusedApproval: (id: string | null) => void;
  setCandidateFilter: (id: string | null) => void;
  openDrawer: (id: string | null) => void;
  addChat: (m: Omit<LocalChat, "id" | "createdAt">) => void;
  setError: (e: string | null) => void;
};

let chatSeq = 0;

const debounce = <F extends (...a: any[]) => void>(fn: F, ms: number) => {
  let t: ReturnType<typeof setTimeout> | null = null;
  return (...args: Parameters<F>) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
};

export const useJobStore = create<JobStore>((set, get) => {
  const refreshAllSoon = debounce(() => {
    get().refreshCandidates();
    get().refreshApprovals();
    get().refreshJob();
  }, 150);

  const dirtyDetails = new Set<string>();
  const refreshDetailsSoon = debounce(() => {
    const ids = [...dirtyDetails];
    dirtyDetails.clear();
    for (const id of ids) if (get().details[id]) get().refreshDetail(id);
  }, 200);

  return {
    jobId: null,
    job: null,
    events: [],
    eventIds: new Set(),
    candidates: [],
    approvals: {},
    details: {},
    chat: [],
    streamStatus: "connecting",
    focusedApprovalId: null,
    candidateFilter: null,
    drawerCandidateId: null,
    error: null,

    connect: (jobId) => {
      set({
        jobId,
        job: null,
        events: [],
        eventIds: new Set(),
        candidates: [],
        approvals: {},
        details: {},
        chat: [],
        focusedApprovalId: null,
        candidateFilter: null,
        drawerCandidateId: null,
        error: null,
      });
      get().refreshJob();
      get().refreshCandidates();
      get().refreshApprovals();
      return subscribeEvents(
        jobId,
        (rows) => {
          const { eventIds, events } = get();
          const fresh = rows.filter((r) => !eventIds.has(r.id));
          if (!fresh.length) return;
          const ids = new Set(eventIds);
          for (const r of fresh) {
            ids.add(r.id);
            if (r.candidateId) dirtyDetails.add(r.candidateId);
          }
          const merged = [...events, ...fresh].sort((a, b) => a.id - b.id);
          set({ events: merged, eventIds: ids });
          refreshAllSoon();
          refreshDetailsSoon();
        },
        (s) => set({ streamStatus: s }),
      );
    },

    refreshJob: async () => {
      const id = get().jobId;
      if (!id) return;
      try {
        set({ job: await api.getJob(id) });
      } catch (e) {
        set({ error: String(e) });
      }
    },
    refreshCandidates: async () => {
      const id = get().jobId;
      if (!id) return;
      try {
        set({ candidates: await api.listCandidates(id) });
      } catch {}
    },
    refreshApprovals: async () => {
      const id = get().jobId;
      if (!id) return;
      try {
        const list = await api.listApprovals(id);
        const approvals: Record<string, Approval> = {};
        for (const a of list) approvals[a.id] = a;
        set({ approvals });
        const f = get().focusedApprovalId;
        if (f && approvals[f]?.status !== "pending") set({ focusedApprovalId: null });
      } catch {}
    },
    refreshDetail: async (candidateId) => {
      try {
        const d = await api.getCandidate(candidateId);
        set({ details: { ...get().details, [candidateId]: d } });
        return d;
      } catch {
        return null;
      }
    },
    setFocusedApproval: (id) => set({ focusedApprovalId: id }),
    setCandidateFilter: (id) => set({ candidateFilter: id }),
    openDrawer: (id) => {
      set({ drawerCandidateId: id });
      if (id) get().refreshDetail(id);
    },
    addChat: (m) =>
      set({
        chat: [...get().chat, { ...m, id: `chat-${++chatSeq}`, createdAt: new Date().toISOString() }],
      }),
    setError: (e) => set({ error: e }),
  };
});

/** Lazily load + subscribe to a candidate's detail (used by interview/evaluation cards). */
export function useCandidateDetail(candidateId: string | null | undefined): CandidateDetail | null {
  const detail = useJobStore((s) => (candidateId ? s.details[candidateId] : undefined));
  const refresh = useJobStore((s) => s.refreshDetail);
  if (candidateId && !detail && !pending.has(candidateId)) {
    pending.add(candidateId);
    queueMicrotask(() => refresh(candidateId).finally(() => pending.delete(candidateId)));
  }
  return detail ?? null;
}
const pending = new Set<string>();
