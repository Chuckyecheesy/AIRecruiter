// Mirror of api/contracts.py (API shapes are camelCase on the wire).
// Keep in sync: when lane A merges a contract change, update this file.

export const CANDIDATE_STATES = [
  "DISCOVERED",
  "RESEARCHED",
  "CONTACT_FOUND",
  "OUTREACH_DRAFTED",
  "APPROVED",
  "CONTACTED",
  "INTERESTED",
  "DECLINED",
  "STALE",
  "INTERVIEWING",
  "INTERVIEWED",
  "EVALUATED",
  "SHORTLISTED",
  "REJECTED",
  "NEEDS_HUMAN",
] as const;
export type CandidateState = (typeof CANDIDATE_STATES)[number];

export type JobState = "DRAFT" | "ACTIVE" | "PAUSED" | "COMPLETE";

export const EVENT_TYPES = [
  "JobCreated",
  "CandidateFound",
  "CandidateEnriched",
  "ContactFound",
  "OutreachDrafted",
  "ApprovalRequested",
  "ApprovalGranted",
  "ApprovalRejected",
  "EmailSent",
  "EmailReplied",
  "InterviewScheduled",
  "InterviewDone",
  "Evaluated",
  "Tick",
  "ToolFailed",
  "Escalated",
  "HumanResolved",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export type ActionName =
  | "search_candidates"
  | "enrich_candidate"
  | "browser_research"
  | "find_contact"
  | "draft_outreach"
  | "redraft_outreach"
  | "draft_reply"
  | "send_outreach"
  | "send_followup"
  | "process_reply"
  | "wait"
  | "mark_stale"
  | "schedule_interview"
  | "process_interview"
  | "evaluate_candidate"
  | "shortlist"
  | "reject_candidate_proposal"
  | "escalate"
  | "resume"
  | "reactivate";

export type RestrictedAction =
  | "send_outreach"
  | "send_followup"
  | "schedule_interview"
  | "reject_candidate_proposal";

export const RESTRICTED_ACTIONS: RestrictedAction[] = [
  "send_outreach",
  "send_followup",
  "schedule_interview",
  "reject_candidate_proposal",
];

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired" | "executed";
export type Disposition = "interested" | "not_interested" | "asks_question" | "no_reply";

export type PermissionPolicy = {
  restrictedActions: string[];
  autoApprove: boolean;
  autoApproveActions: string[];
  approvalTimeoutMinutes: number | null;
};

export type JobForm = {
  title: string;
  mustHaves: string[];
  niceToHaves: string[];
  location: string | null;
  seniority: string | null;
  freeText: string | null;
};

export type RubricCriterion = { name: string; weight: number; description?: string | null };
export type Rubric = { criteria: RubricCriterion[]; interviewThemes: string[] };

export type DecisionLogEntry = { ts: string; action: string; reasoning: string; outcome: string | null };
export type ContactLogEntry = { ts: string; channel: string; action: string; messageId: string | null };

export type CandidateMemory = {
  candidateId: string;
  profile: Record<string, any>;
  researchNotes: Record<string, any>;
  contact: Record<string, any>;
  contactLog: ContactLogEntry[];
  decisions: DecisionLogEntry[];
  summary: string;
  updatedAt: string | null;
};

export type Candidate = {
  id: string;
  name: string;
  state: CandidateState;
  score: number | null;
  headline: string | null;
  needsHuman: boolean;
  traceUrl: string | null;
  pendingApprovalId: string | null;
  demoDisposition: Disposition | null;
  updatedAt: string;
};

export type Approval = {
  id: string;
  jobId: string;
  candidateId: string;
  candidateName: string;
  action: RestrictedAction;
  payload: Record<string, any>;
  reasoning: string;
  status: ApprovalStatus;
  decidedBy: "ceo" | "auto" | null;
  feedback: string | null;
  createdAt: string;
  decidedAt: string | null;
};

export type EventRow = {
  id: number;
  type: EventType;
  candidateId: string | null;
  candidateName: string | null;
  approvalId: string | null;
  payload: Record<string, any>;
  createdAt: string;
};

export type JobSummary = {
  id: string;
  title: string;
  state: JobState;
  counts: Partial<Record<CandidateState, number>>;
  pendingApprovals: number;
  createdAt: string;
  updatedAt: string;
};

export type JobDetail = JobSummary & {
  form: JobForm;
  rubric: Rubric | null;
  shortlistTarget: number;
  scoreThreshold: number;
  maxCandidates: number;
  permissionPolicy: PermissionPolicy;
};

export type Message = {
  id: string;
  channel: "email" | "voice";
  direction: "out" | "in";
  subject: string | null;
  body: string | null;
  simulated: boolean;
  createdAt: string;
};

export type TranscriptTurn = { role: string; text?: string; message?: string; [k: string]: unknown };

export type Interview = {
  id: string;
  conversationId: string | null;
  joinUrl: string | null;
  expiresAt: string | null;
  questions: string[];
  status: "scheduled" | "in_progress" | "completed" | "expired";
  transcript: TranscriptTurn[];
  durationS: number | null;
  createdAt: string;
  completedAt: string | null;
};

export type CriterionScore = { name: string; weight: number; score: number; evidence: string };

export type Evaluation = {
  id: string;
  score: number;
  criteria: CriterionScore[];
  recommendation: "shortlist" | "reject" | "borderline";
  summary: string;
  createdAt: string;
};

export type CandidateDetail = Candidate & {
  jobId: string;
  prevState: CandidateState | null;
  memory: CandidateMemory | null;
  messages: Message[];
  interview: Interview | null;
  evaluation: Evaluation | null;
  events: EventRow[];
  approvals: Approval[];
};

export type ShortlistEntry = { candidate: Candidate; evaluation: Evaluation | null };

export type JobCreateIn = Partial<Omit<JobForm, "title">> & { title: string };
export type JobPatchIn = Partial<{
  title: string;
  rubric: Rubric;
  shortlistTarget: number;
  scoreThreshold: number;
  maxCandidates: number;
  permissionPolicy: Partial<PermissionPolicy>;
}>;


// Pipeline column groups (spec 06)
export const PIPELINE_COLUMNS: { title: string; states: CandidateState[] }[] = [
  { title: "Sourcing", states: ["DISCOVERED", "RESEARCHED", "CONTACT_FOUND"] },
  { title: "Outreach", states: ["OUTREACH_DRAFTED", "APPROVED", "CONTACTED"] },
  { title: "Interview", states: ["INTERESTED", "INTERVIEWING", "INTERVIEWED"] },
  { title: "Scored", states: ["EVALUATED", "SHORTLISTED"] },
  { title: "Closed", states: ["REJECTED", "DECLINED", "STALE"] },
];
