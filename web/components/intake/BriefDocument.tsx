"use client";

import type { FC, ReactNode } from "react";
import type { RoleDraft } from "@/lib/voice/types";
import { cn } from "@/lib/utils";

type Props = {
  draft: RoleDraft;
  /** Show the pulsing "live" marker while the call is running. */
  live?: boolean;
  lastUpdated?: (keyof RoleDraft)[];
  /** e.g. "Nov 22, 2026"; set when the call starts (kept out of render so prerendering stays deterministic) */
  dateLabel?: string;
  className?: string;
  footer?: ReactNode;
};

const Empty: FC<{ w?: string }> = ({ w = "w-32" }) => (
  <span aria-label="Not captured yet" className={cn("inline-block h-[0.6em] translate-y-[0.1em] rounded-full border-b-2 border-dotted border-line", w)} />
);

const Row: FC<{ label: string; hot?: boolean; children: ReactNode }> = ({ label, hot, children }) => (
  <div className="grid grid-cols-[7.5rem_1fr] items-baseline gap-3 py-2.5">
    <dt className={cn("label-mono transition-colors duration-700", hot ? "text-gate" : "text-quiet")}>{label}</dt>
    <dd className="min-w-0 text-[15px] text-ink">{children}</dd>
  </div>
);

/** The role brief rendered as a memo. Same fields as `toBriefMarkdown`, so screen and file match. */
export const BriefDocument: FC<Props> = ({ draft, live, lastUpdated = [], dateLabel, className, footer }) => {
  const hot = (k: keyof RoleDraft) => lastUpdated.includes(k);

  return (
    <article className={cn("relative rounded-2xl border border-line bg-surface px-6 pt-5 pb-6 shadow-[0_1px_0_#eadfd0,0_24px_48px_-32px_rgba(43,36,32,0.25)]", className)}>
      <header className="flex items-center justify-between">
        <span className="label-mono flex items-center gap-2 text-quiet">
          {live && (
            <span className="relative flex size-2">
              <span className="absolute inset-0 animate-ping rounded-full bg-gate/60" />
              <span className="relative size-2 rounded-full bg-gate" />
            </span>
          )}
          Brief{live ? " · live" : ""}
        </span>
        {dateLabel && <span className="label-mono text-quiet">{dateLabel}</span>}
      </header>

      <h2 className="mt-4 min-h-[2.5rem] text-[1.75rem] leading-tight font-semibold text-ink">
        {draft.title ? (
          <span key={draft.title} className="animate-rise inline-block">
            {draft.title}
          </span>
        ) : (
          <span className="text-quiet/50">Role title…</span>
        )}
      </h2>

      <dl className="mt-3 divide-y divide-line/70 border-t border-line/70">
        <Row label="Must-have" hot={hot("mustHaves")}>
          {draft.mustHaves.length ? (
            <ul className="flex flex-wrap gap-1.5">
              {draft.mustHaves.map((s) => (
                <li key={s} className="animate-rise rounded-full bg-ink px-2.5 py-0.5 text-sm text-surface">
                  {s}
                </li>
              ))}
            </ul>
          ) : (
            <Empty w="w-40" />
          )}
        </Row>
        <Row label="Nice to have" hot={hot("niceToHaves")}>
          {draft.niceToHaves.length ? (
            <ul className="flex flex-wrap gap-1.5">
              {draft.niceToHaves.map((s) => (
                <li key={s} className="animate-rise rounded-full border border-ink/25 px-2.5 py-0.5 text-sm text-ink">
                  {s}
                </li>
              ))}
            </ul>
          ) : (
            <Empty />
          )}
        </Row>
        <Row label="Location" hot={hot("location")}>
          {draft.location ? (
            <span key={draft.location} className="animate-rise inline-block">
              {draft.location}
            </span>
          ) : (
            <Empty w="w-24" />
          )}
        </Row>
        <Row label="Seniority" hot={hot("seniority")}>
          {draft.seniority ? (
            <span key={draft.seniority} className="animate-rise inline-block capitalize">
              {draft.seniority}
            </span>
          ) : (
            <Empty w="w-16" />
          )}
        </Row>
        <Row label="Notes" hot={hot("notes")}>
          {draft.notes ? (
            <ul className="grid gap-1 text-quiet">
              {draft.notes.split("\n").map((n, i) => (
                <li key={`${i}-${n}`} className="animate-rise">
                  {n}
                </li>
              ))}
            </ul>
          ) : (
            <Empty w="w-48" />
          )}
        </Row>
      </dl>
      {footer && <div className="mt-5">{footer}</div>}
    </article>
  );
};
