import { api, streamUrl } from "./api";
import type { EventRow } from "./types";

export type StreamStatus = "connecting" | "live" | "polling";

/**
 * Subscribe to a job's events: SSE first, falling back to polling /events every 2s
 * if SSE errors. Callers dedupe by id (delayed events can arrive with an older id).
 */
export function subscribeEvents(
  jobId: string,
  onEvents: (events: EventRow[]) => void,
  onStatus?: (s: StreamStatus) => void,
): () => void {
  let closed = false;
  let cursor = 0;
  let es: EventSource | null = null;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let sseFailures = 0;

  const accept = (rows: EventRow[]) => {
    if (!rows.length) return;
    for (const r of rows) cursor = Math.max(cursor, r.id);
    onEvents(rows);
  };

  const poll = async () => {
    if (closed) return;
    try {
      accept(await api.events(jobId, cursor));
    } catch {
      /* keep polling */
    }
    if (!closed) pollTimer = setTimeout(poll, 2000);
  };

  const startPolling = () => {
    onStatus?.("polling");
    poll();
  };

  const openSse = () => {
    if (closed) return;
    onStatus?.("connecting");
    es = new EventSource(streamUrl(jobId, cursor));
    // Batch bursts (the initial history arrives as hundreds of messages).
    let batch: EventRow[] = [];
    let flush: ReturnType<typeof setTimeout> | null = null;
    es.addEventListener("open", () => {
      sseFailures = 0;
      onStatus?.("live");
    });
    es.addEventListener("event", (e) => {
      batch.push(JSON.parse((e as MessageEvent).data));
      if (!flush)
        flush = setTimeout(() => {
          const rows = batch;
          batch = [];
          flush = null;
          accept(rows);
        }, 50);
    });
    es.addEventListener("error", () => {
      es?.close();
      es = null;
      sseFailures += 1;
      if (closed) return;
      if (sseFailures >= 3) startPolling();
      else setTimeout(openSse, 1000 * sseFailures);
    });
  };

  if (typeof EventSource === "undefined") startPolling();
  else openSse();

  return () => {
    closed = true;
    es?.close();
    if (pollTimer) clearTimeout(pollTimer);
  };
}
