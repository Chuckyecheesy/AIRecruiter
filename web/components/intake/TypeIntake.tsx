"use client";

import { useState, type FC, type FormEvent, type ReactNode } from "react";
import type { RoleDraft } from "@/lib/voice/types";
import { cn } from "@/lib/utils";

export const fieldClass =
  "w-full rounded-xl border border-line bg-surface px-3.5 py-2.5 text-[15px] text-ink outline-none transition-colors placeholder:text-quiet/60 focus:border-ink/60";

const splitList = (s: string) =>
  s
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);

const Field: FC<{ label: string; children: ReactNode; className?: string }> = ({ label, children, className }) => (
  <label className={cn("grid gap-1.5", className)}>
    <span className="label-mono text-quiet">{label}</span>
    {children}
  </label>
);

type Props = {
  initial: RoleDraft;
  busy: boolean;
  err: string | null;
  onSubmit: (draft: RoleDraft) => void;
};

export const TypeIntake: FC<Props> = ({ initial, busy, err, onSubmit }) => {
  const [title, setTitle] = useState(initial.title);
  const [must, setMust] = useState(initial.mustHaves.join(", "));
  const [nice, setNice] = useState(initial.niceToHaves.join(", "));
  const [location, setLocation] = useState(initial.location);
  const [seniority, setSeniority] = useState(initial.seniority);
  const [notes, setNotes] = useState(initial.notes);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    onSubmit({ title, mustHaves: splitList(must), niceToHaves: splitList(nice), location, seniority, notes });
  };

  return (
    <form onSubmit={submit} className="grid max-w-3xl gap-5 rounded-2xl border border-line bg-surface/60 p-6">
      <Field label="Role title">
        <input className={fieldClass} value={title} onChange={(e) => setTitle(e.target.value)} required />
      </Field>
      <div className="grid gap-5 sm:grid-cols-2">
        <Field label="Must-have skills">
          <input className={fieldClass} value={must} onChange={(e) => setMust(e.target.value)} placeholder="Comma separated" />
        </Field>
        <Field label="Nice to have">
          <input className={fieldClass} value={nice} onChange={(e) => setNice(e.target.value)} placeholder="Comma separated" />
        </Field>
        <Field label="Location">
          <input className={fieldClass} value={location} onChange={(e) => setLocation(e.target.value)} />
        </Field>
        <Field label="Seniority">
          <select className={fieldClass} value={seniority} onChange={(e) => setSeniority(e.target.value)}>
            <option value="">Any</option>
            <option value="junior">Junior</option>
            <option value="mid">Mid</option>
            <option value="senior">Senior</option>
            <option value="staff">Staff</option>
          </select>
        </Field>
      </div>
      <Field label="Anything else the recruiter should know">
        <textarea className={cn(fieldClass, "min-h-24")} value={notes} onChange={(e) => setNotes(e.target.value)} />
      </Field>
      {err && <p className="text-sm text-danger">{err}</p>}
      <div>
        <button type="submit" disabled={busy} className="rounded-full bg-gate px-5 py-2.5 font-medium text-surface transition hover:bg-gate-deep disabled:opacity-60">
          {busy ? "Writing the rubric…" : "Generate rubric →"}
        </button>
      </div>
    </form>
  );
};
