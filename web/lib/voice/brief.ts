import type { RoleDraft, TranscriptLine } from "./types";

const fmtDate = (d: Date) => d.toISOString().slice(0, 10);

const list = (items: string[]) => (items.length ? items.map((x) => `- ${x}`).join("\n") : "_Not specified_");

/** The role brief as markdown. This is the file the CEO downloads, and the text the rubric generator receives. */
export function toBriefMarkdown(
  draft: RoleDraft,
  transcript: TranscriptLine[],
  source: "voice" | "typed",
  now: Date = new Date(),
): string {
  const parts: string[] = [];
  parts.push(`# Role brief: ${draft.title || "Untitled role"}`);
  parts.push(`_${source === "voice" ? "Captured by voice" : "Written"} on ${fmtDate(now)}_`);
  parts.push(`## Must-haves\n\n${list(draft.mustHaves)}`);
  parts.push(`## Nice to have\n\n${list(draft.niceToHaves)}`);
  parts.push(
    `## Logistics\n\n- **Location:** ${draft.location || "_Not specified_"}\n- **Seniority:** ${draft.seniority || "_Not specified_"}`,
  );
  if (draft.notes.trim()) parts.push(`## Notes from the CEO\n\n${draft.notes.trim()}`);
  if (transcript.length) {
    const lines = transcript.map((t) => `**${t.role === "ceo" ? "CEO" : "Recruiter"}:** ${t.text}`).join("\n\n");
    parts.push(`## Transcript\n\n${lines}`);
  }
  return parts.join("\n\n") + "\n";
}

/** Brief text sent as `freeText`: everything except the transcript, which is long and mostly repeats the brief. */
export function toBriefFreeText(draft: RoleDraft, transcript: TranscriptLine[]): string {
  const md = toBriefMarkdown(draft, [], "voice");
  const ceoOnly = transcript.filter((t) => t.role === "ceo").map((t) => `> ${t.text}`);
  return ceoOnly.length ? `${md}\n## What the CEO said\n\n${ceoOnly.join("\n>\n")}\n` : md;
}

export function briefFileName(draft: RoleDraft): string {
  const slug = (draft.title || "role")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48);
  return `brief-${slug || "role"}.md`;
}

export function downloadMarkdown(fileName: string, markdown: string) {
  const url = URL.createObjectURL(new Blob([markdown], { type: "text/markdown;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
