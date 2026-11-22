/** Role draft shared by voice and typed intake. Maps onto `JobCreateIn` at submit time. */
export type RoleDraft = {
  title: string;
  mustHaves: string[];
  niceToHaves: string[];
  location: string;
  seniority: string;
  /** Anything else the CEO said that the recruiter should know. */
  notes: string;
};

export const EMPTY_DRAFT: RoleDraft = {
  title: "",
  mustHaves: [],
  niceToHaves: [],
  location: "",
  seniority: "",
  notes: "",
};

export type TranscriptLine = {
  role: "ceo" | "agent";
  text: string;
  /** ISO timestamp */
  at: string;
};

export type VoiceStatus = "idle" | "connecting" | "listening" | "speaking" | "ended" | "error";

/** Parameters of the `update_role_brief` client tool (see INTAKE_AGENT.md). All optional; only sent fields change. */
export type BriefUpdate = {
  title?: string;
  must_haves?: string[] | string;
  nice_to_haves?: string[] | string;
  location?: string;
  seniority?: string;
  notes?: string;
};

export type IntakeVoice = {
  /** "mock" replays a scripted call; "elevenlabs" is the live agent. */
  mode: "mock" | "elevenlabs";
  status: VoiceStatus;
  error: string | null;
  transcript: TranscriptLine[];
  draft: RoleDraft;
  /** Fields captured most recently, so the brief can highlight them. */
  lastUpdated: (keyof RoleDraft)[];
  start: () => void;
  stop: () => void;
  reset: () => void;
  /** 0..1, read every animation frame */
  inputLevel: () => number;
  outputLevel: () => number;
};

const asList = (v: string[] | string | undefined): string[] | undefined => {
  if (v === undefined) return undefined;
  const arr = Array.isArray(v) ? v : v.split(/[,\n;]/);
  return arr.map((x) => String(x).trim()).filter(Boolean);
};

/** Merge a client-tool update into the draft. List fields are merged as a set, notes are appended. */
export function applyBriefUpdate(draft: RoleDraft, u: BriefUpdate): { draft: RoleDraft; changed: (keyof RoleDraft)[] } {
  const next = { ...draft };
  const changed: (keyof RoleDraft)[] = [];
  const setStr = (k: "title" | "location" | "seniority", v?: string) => {
    if (v && v.trim() && v.trim() !== next[k]) {
      next[k] = v.trim();
      changed.push(k);
    }
  };
  setStr("title", u.title);
  setStr("location", u.location);
  setStr("seniority", u.seniority?.toLowerCase());
  const mergeList = (k: "mustHaves" | "niceToHaves", v?: string[]) => {
    if (!v?.length) return;
    const seen = new Set(next[k].map((x) => x.toLowerCase()));
    const add = v.filter((x) => !seen.has(x.toLowerCase()));
    if (add.length) {
      next[k] = [...next[k], ...add];
      changed.push(k);
    }
  };
  mergeList("mustHaves", asList(u.must_haves));
  mergeList("niceToHaves", asList(u.nice_to_haves));
  if (u.notes && u.notes.trim()) {
    next.notes = next.notes ? `${next.notes}\n${u.notes.trim()}` : u.notes.trim();
    changed.push("notes");
  }
  return { draft: next, changed };
}
