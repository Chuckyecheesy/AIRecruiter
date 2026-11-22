"use client";

import { useState, type FC } from "react";
import { api } from "@/lib/api";
import { useJobStore } from "@/lib/store";
import { cn } from "@/lib/utils";

/** Auto-approve on/off. Takes effect on the agent's next permission check. */
export const PolicyToggle: FC = () => {
  const job = useJobStore((s) => s.job);
  const refreshJob = useJobStore((s) => s.refreshJob);
  const [busy, setBusy] = useState(false);
  if (!job) return null;
  const on = job.permissionPolicy.autoApprove;

  const toggle = async () => {
    setBusy(true);
    try {
      await api.patchJob(job.id, { permissionPolicy: { ...job.permissionPolicy, autoApprove: !on } });
      await refreshJob();
    } finally {
      setBusy(false);
    }
  };

  return (
    <label className="flex cursor-pointer items-center gap-2 text-sm">
      <span className="text-ink">Auto-approve</span>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        disabled={busy}
        onClick={toggle}
        className={cn(
          "relative h-6 w-11 rounded-full transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gate",
          on ? "bg-agent" : "bg-[#e2d5c3]",
        )}
      >
        <span className={cn("absolute top-0.5 size-5 rounded-full bg-surface shadow-sm transition-[left]", on ? "left-[22px]" : "left-0.5")} />
      </button>
      <span className="w-28 text-xs text-quiet">{on ? "Agent acts alone (rejections still ask)" : "Agent asks first"}</span>
    </label>
  );
};
