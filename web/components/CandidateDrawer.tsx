"use client";

import { useEffect, useState, type FC } from "react";
import { api } from "@/lib/api";
import { useJobStore } from "@/lib/store";
import type { CandidateDetail, Disposition } from "@/lib/types";
import { cn } from "@/lib/utils";
import { EvaluationBody } from "./tool-uis";

const TABS = ["Memory", "Timeline", "Conversation", "Interview", "Evaluation"] as const;
type Tab = (typeof TABS)[number];

export const CandidateDrawer: FC = () => {
  const id = useJobStore((s) => s.drawerCandidateId);
  const detail = useJobStore((s) => (id ? s.details[id] : null));
  const close = useJobStore((s) => s.openDrawer);
  const [tab, setTab] = useState<Tab>("Memory");

  useEffect(() => {
    if (!id) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [id, close]);

  if (!id) return null;

  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true" aria-label={detail?.name ?? "Candidate"}>
      <button type="button" aria-label="Close" className="absolute inset-0 bg-ink/20" onClick={() => close(null)} />
      <aside className="relative flex h-full w-full max-w-xl flex-col bg-white shadow-xl">
        <header className="border-b border-line px-5 pt-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="truncate text-lg font-semibold text-ink">{detail?.name ?? "Loading…"}</h2>
              {detail?.headline && <p className="truncate text-sm text-quiet">{detail.headline}</p>}
            </div>
            <button type="button" onClick={() => close(null)} className="rounded px-2 py-1 text-sm text-quiet hover:bg-canvas">
              Close
            </button>
          </div>
          {detail && <NeedsHumanBanner d={detail} />}
          <nav className="-mb-px mt-3 flex gap-4 overflow-x-auto" aria-label="Candidate sections">
            {TABS.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={cn("border-b-2 pb-2 text-sm whitespace-nowrap", tab === t ? "border-ink text-ink" : "border-transparent text-quiet hover:text-ink")}
              >
                {t}
              </button>
            ))}
          </nav>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-4">{detail ? <TabBody tab={tab} d={detail} /> : null}</div>
      </aside>
    </div>
  );
};

const NeedsHumanBanner: FC<{ d: CandidateDetail }> = ({ d }) => {
  const refresh = useJobStore((s) => s.refreshDetail);
  const refreshCandidates = useJobStore((s) => s.refreshCandidates);
  if (d.state !== "NEEDS_HUMAN" && d.state !== "STALE") return null;
  const act = async (fn: () => Promise<unknown>) => {
    await fn();
    await Promise.all([refresh(d.id), refreshCandidates()]);
  };
  const lastError = [...d.events].reverse().find((e) => e.type === "Escalated")?.payload?.error;
  return (
    <div className="mt-3 rounded-md border border-danger/30 bg-danger/5 p-3 text-sm">
      <p className="text-ink">{d.state === "STALE" ? "No reply after the follow-up." : (lastError ?? "The agent stopped and needs you.")}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {d.traceUrl && (
          <a href={d.traceUrl} target="_blank" rel="noreferrer" className="text-agent underline underline-offset-2">
            Open trace in Langfuse
          </a>
        )}
        <button type="button" onClick={() => act(() => api.resumeCandidate(d.id))} className="rounded-md bg-ink px-3 py-1 text-white">
          {d.state === "STALE" ? "Reactivate" : "Resume"}
        </button>
        <button type="button" onClick={() => act(() => api.skipCandidate(d.id))} className="rounded-md border border-line px-3 py-1">
          Skip
        </button>
      </div>
    </div>
  );
};

const Field: FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="py-2">
    <dt className="text-sm text-quiet">{label}</dt>
    <dd className="mt-0.5 text-[15px] text-ink">{children}</dd>
  </div>
);

const DISPOSITIONS: Disposition[] = ["interested", "not_interested", "asks_question", "no_reply"];

const TabBody: FC<{ tab: Tab; d: CandidateDetail }> = ({ tab, d }) => {
  const refresh = useJobStore((s) => s.refreshDetail);
  const m = d.memory;
  const profile = m?.profile ?? {};

  if (tab === "Memory")
    return (
      <dl className="divide-y divide-line">
        {m?.summary && <Field label="Summary">{m.summary}</Field>}
        {profile.skills?.length > 0 && <Field label="Skills">{profile.skills.join(", ")}</Field>}
        {profile.projects?.length > 0 && (
          <Field label="Projects">
            <ul className="list-disc pl-5">
              {profile.projects.map((p: any, i: number) => (
                <li key={i}>{typeof p === "string" ? p : `${p.name}${p.description ? `: ${p.description}` : ""}`}</li>
              ))}
            </ul>
          </Field>
        )}
        {profile.education?.length > 0 && <Field label="Education">{profile.education.join(", ")}</Field>}
        {profile.links && Object.keys(profile.links).length > 0 && (
          <Field label="Links">
            <ul>
              {Object.entries(profile.links).map(([k, v]) => (
                <li key={k}>
                  <a href={String(v)} target="_blank" rel="noreferrer" className="text-agent underline underline-offset-2">
                    {k}
                  </a>
                </li>
              ))}
            </ul>
          </Field>
        )}
        {profile.completeness != null && <Field label="Profile completeness">{Math.round(profile.completeness * 100)}%</Field>}
        {m?.contact?.email && (
          <Field label="Contact">
            {m.contact.email} <span className="text-sm text-quiet">({m.contact.source ?? "unknown source"})</span>
          </Field>
        )}
        {m && Object.keys(m.researchNotes ?? {}).length > 0 && (
          <Field label="Research notes">
            <pre className="whitespace-pre-wrap text-sm">{JSON.stringify(m.researchNotes, null, 2)}</pre>
          </Field>
        )}
        <Field label="Demo reply (pinned)">
          <select
            value={d.demoDisposition ?? ""}
            onChange={async (e) => {
              await api.setDisposition(d.id, (e.target.value || null) as Disposition | null);
              refresh(d.id);
            }}
            className="rounded-md border border-line bg-white px-2 py-1 text-sm"
          >
            <option value="">Random</option>
            {DISPOSITIONS.map((x) => (
              <option key={x} value={x}>
                {x.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </Field>
      </dl>
    );

  if (tab === "Timeline")
    return (
      <ol className="space-y-3">
        {(m?.decisions ?? []).map((x, i) => (
          <li key={i} className="text-sm">
            <p className="text-ink">
              <span className="font-medium">{x.action.replaceAll("_", " ")}</span>
              <span className="text-quiet"> at {new Date(x.ts).toLocaleTimeString()}</span>
            </p>
            {x.reasoning && <p className="text-quiet">{x.reasoning}</p>}
          </li>
        ))}
        {!m?.decisions?.length && <p className="text-sm text-quiet">No decisions yet.</p>}
      </ol>
    );

  if (tab === "Conversation")
    return d.messages.length ? (
      <ol className="space-y-3">
        {d.messages.map((msg) => (
          <li key={msg.id} className={cn("rounded-md border p-3", msg.direction === "out" ? "border-line bg-white" : "border-agent/20 bg-agent/5")}>
            <p className="text-xs text-quiet">
              {msg.direction === "out" ? "We wrote" : `${d.name} replied`} at {new Date(msg.createdAt).toLocaleTimeString()}
              {msg.simulated && " (simulated)"}
            </p>
            {msg.subject && <p className="mt-1 text-sm font-medium text-ink">{msg.subject}</p>}
            <p className="mt-1 whitespace-pre-wrap text-[15px] text-ink">{msg.body}</p>
          </li>
        ))}
      </ol>
    ) : (
      <p className="text-sm text-quiet">No emails yet.</p>
    );

  if (tab === "Interview") {
    const iv = d.interview;
    if (!iv) return <p className="text-sm text-quiet">No interview scheduled.</p>;
    return (
      <div className="space-y-3 text-sm">
        <p className="text-ink">Status: {iv.status.replace("_", " ")}</p>
        {iv.joinUrl && iv.status !== "completed" && (
          <div className="flex flex-wrap items-center gap-2">
            <a href={iv.joinUrl} target="_blank" rel="noreferrer" className="text-agent underline underline-offset-2">
              Open interview
            </a>
            <button type="button" onClick={() => navigator.clipboard.writeText(iv.joinUrl!)} className="rounded-md border border-line px-3 py-1">
              Copy join link
            </button>
          </div>
        )}
        {iv.questions.length > 0 && (
          <ol className="list-decimal pl-5 text-ink">
            {iv.questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ol>
        )}
        {iv.transcript.length > 0 && (
          <ol className="space-y-1.5 border-t border-line pt-3">
            {iv.transcript.map((t, i) => (
              <li key={i} className={t.role === "agent" ? "text-quiet" : "text-ink"}>
                <span className="font-medium">{t.role === "agent" ? "Interviewer" : "Candidate"}:</span> {t.text ?? t.message}
              </li>
            ))}
          </ol>
        )}
      </div>
    );
  }

  return d.evaluation ? <EvaluationBody evaluation={d.evaluation} /> : <p className="text-sm text-quiet">Not evaluated yet.</p>;
};
