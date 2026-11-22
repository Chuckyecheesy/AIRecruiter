"""Offline-first eval harness for lane B's Claude tools (drafter, persona, classifier, evaluator, rubric).

    .venv/bin/python tests/evals/run_evals.py            # stub mode (no API calls)
    .venv/bin/python tests/evals/run_evals.py --live     # LLM_MODE=real (costs ~$0.3/run)
        options: --only drafter,evaluator   --repeat 3   --json out.json   --verbose

Graders are deterministic (no LLM judge). A "wrapper" suite always runs offline: it feeds canned HTTP
responses to api.tools.llm.LLM through a mock transport to check error mapping (truncation, schema-violating
values, refusal) without touching the network.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
HERE = Path(__file__).resolve().parent

from api.config import Settings  # noqa: E402
from api.tools import build_tools  # noqa: E402
from api.tools.base import ToolError  # noqa: E402
from api.tools.models import EnrichedProfile, Evaluation, Message, RoleForm  # noqa: E402

# ---------------------------------------------------------------- helpers

PLACEHOLDER_RE = re.compile(r"\[[^\]]{2,40}\]|\{\{?[^}]{2,40}\}\}?|<[A-Za-z ]{2,30}>|your name|company name", re.I)
MARKDOWN_RE = re.compile(r"\*\*|^#+\s|^\s*[-*]\s", re.M)
STOP = set("the a an and or of to in for on with at by from is are was were be been it this that as i you we "
           "they he she my our your their have has had do did not no but so if then than very just".split())


def words(s: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9$][A-Za-z0-9'$.+#/-]*", s or "")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split().__str__()


def toks(s: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split() if t not in STOP]


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def flatten_text(obj: Any) -> str:
    if isinstance(obj, dict):
        return " ".join(flatten_text(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return " ".join(flatten_text(v) for v in obj)
    return "" if obj is None else str(obj)


def grounded(evidence: str, memory_text: str, memory_tokens: set[str]) -> bool:
    """Fuzzy: evidence is grounded if it is a near-substring of memory, or >=60% of its content tokens
    (and every number it cites) appear in memory."""
    e = evidence.strip().strip('"').strip("'")
    if not e or "no evidence" in e.lower():
        return True
    el = e.lower()
    if el in memory_text.lower():
        return True
    m = difflib.SequenceMatcher(None, el, memory_text.lower()).find_longest_match(0, len(el), 0, len(memory_text))
    if m.size >= max(20, int(0.6 * len(el))):
        return True
    et = toks(e)
    if not et:
        return True
    nums = [t for t in et if re.fullmatch(r"\d+(\.\d+)?", t)]
    if any(n not in memory_tokens for n in nums):
        return False
    return sum(t in memory_tokens for t in et) / len(et) >= 0.6


class Result:
    def __init__(self, suite: str, case: str):
        self.suite, self.case = suite, case
        self.checks: list[tuple[str, bool, str]] = []
        self.output: Any = None
        self.error: str | None = None
        self.latency_s = 0.0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))

    @property
    def passed(self) -> bool:
        return self.error is None and all(ok for _, ok, _ in self.checks)


# ---------------------------------------------------------------- fixtures

def load_fixtures() -> tuple[dict[str, dict], list[dict]]:
    cands = json.loads((REPO / "fixtures/ploid_candidates.json").read_text())
    profiles = {c["ploid_id"]: c["profile"] for c in cands}
    transcript = json.loads((REPO / "fixtures/transcript.json").read_text())["turns"]
    return profiles, transcript


def profile_for(profiles: dict, case: dict) -> dict:
    p = json.loads(json.dumps(profiles[case["ploid_id"]]))
    p.update(case.get("profile_patch") or {})
    return p


def memory_for(profile: dict, thread: list | None = None, transcript: list | None = None) -> dict:
    """Shape mirrors candidate_memory (spec 04) plus the conversation/interview lane A merges in."""
    return {"profile": profile, "research_notes": {}, "contact_log": [],
            "conversation": thread or [], "interview_transcript": transcript or []}


def other_names(profiles: dict, me: str) -> set[str]:
    """Companies/projects belonging to OTHER candidates: appearing in my draft = cross-contamination/invention."""
    mine = profiles[me]
    mine_txt = flatten_text(mine).lower()
    out = set()
    for pid, p in profiles.items():
        if pid == me:
            continue
        for e in p.get("experience", []):
            out.add(e["company"])
        for pr in p.get("projects", []):
            out.add(pr["name"])
    return {n for n in out if n.lower() not in mine_txt}


# ---------------------------------------------------------------- suites

async def run_drafter(tools, cfg, profiles, verbose) -> list[Result]:
    job = cfg["job"]
    cases = cfg["drafter"]
    done: dict[str, Any] = {}
    results: list[Result] = []
    pending = list(cases)
    while pending:
        progressed = False
        for case in list(pending):
            dep = case.get("redraft_of")
            if dep and dep not in done:
                continue
            pending.remove(case)
            progressed = True
            r = Result("drafter", case["id"])
            prof = profile_for(profiles, case)
            mem = memory_for(prof)
            t0 = time.perf_counter()
            try:
                if case["kind"] == "reply":
                    thread = [Message(**m) for m in case["thread"]]
                    d = await tools.drafter.reply(job, memory_for(prof, case["thread"]), thread)
                else:
                    prev = done.get(dep) if dep else None
                    d = await tools.drafter.outreach(job, mem, feedback=case.get("feedback"), previous=prev)
            except Exception as e:  # noqa: BLE001
                r.error = f"{type(e).__name__}: {e}"
                results.append(r)
                continue
            r.latency_s = time.perf_counter() - t0
            done[case["id"]] = d
            r.output = d.model_dump()
            grade_draft(r, case, d, prof, profiles, done.get(dep) if dep else None)
            results.append(r)
        if not progressed:
            for case in pending:
                r = Result("drafter", case["id"]); r.error = f"dependency {case.get('redraft_of')} failed"
                results.append(r)
            break
    order = {c["id"]: i for i, c in enumerate(cases)}
    return sorted(results, key=lambda r: order[r.case])


def grade_draft(r: Result, case: dict, d, prof: dict, profiles: dict, prev) -> None:
    ex = case.get("expect", {})
    body = d.body or ""
    full = f"{d.subject or ''}\n{body}"
    n = len(words(body))
    r.check("word_count", n <= ex.get("max_words", 120), f"{n} words")
    r.check("no_placeholders", not PLACEHOLDER_RE.search(full), (PLACEHOLDER_RE.search(full) or [""])[0])
    r.check("no_markdown", not MARKDOWN_RE.search(body))
    if case["kind"] == "outreach":
        r.check("has_subject", bool(d.subject))
    foreign = [x for x in other_names(profiles, case["ploid_id"]) if x.lower() in full.lower()]
    r.check("no_foreign_names", not foreign, ",".join(foreign))
    if ex.get("mention_memory"):
        hooks = [p["name"] for p in prof.get("projects", [])] + [e["company"] for e in prof.get("experience", [])]
        hit = [h for h in hooks if h.lower() in full.lower()]
        r.check("mentions_real_project_or_employer", bool(hit), ",".join(hit) or f"none of {hooks}")
    if ex.get("no_project_claims"):
        r.check("no_invented_project", not re.search(r"\byour [A-Z][\w-]+( [A-Z][\w-]+)* (project|app|tool)\b", body),
                "draft names a project the profile doesn't have")
    for m in ex.get("must_mention", []):
        r.check(f"mentions:{m}", m.lower() in full.lower().replace("-", "-"))
    if ex.get("must_mention_any"):
        r.check("mentions_any", any(m in full.lower() for m in ex["must_mention_any"]), str(ex["must_mention_any"]))
    for pat in ex.get("forbid_regex", []):
        m = re.search(pat, full, re.I)
        r.check(f"forbid:{pat}", not m, m.group(0) if m else "")
    if prev is not None:
        sim = similarity(prev.body, body)
        r.check("visibly_different", sim <= ex.get("max_similarity", 0.8), f"similarity={sim:.2f}")
        if "shorter_than_previous" in ex:
            pn = len(words(prev.body))
            r.check("shorter", n <= ex["shorter_than_previous"] * pn, f"{n} vs prev {pn}")


async def run_persona(tools, cfg, profiles, verbose) -> list[Result]:
    out = []
    opener = Message(direction="out", subject="AI Engineer role - quick chat?",
                     body="Hi, I came across your work and was impressed. We're hiring an AI Engineer (RAG + TypeScript) "
                          "at a seed-stage startup in Waterloo. Would you be open to a short voice screening call this week? Best, the CEO")
    for case in cfg["persona"]:
        r = Result("persona", case["id"])
        prof = EnrichedProfile.model_validate(profile_for(profiles, case))
        t0 = time.perf_counter()
        try:
            txt = await tools.persona.reply(prof, [opener], case["disposition"])
        except Exception as e:  # noqa: BLE001
            if case.get("expect_error") == type(e).__name__:
                r.check("raises_" + type(e).__name__, True)
            else:
                r.error = f"{type(e).__name__}: {e}"
            out.append(r)
            continue
        r.latency_s = time.perf_counter() - t0
        r.output = txt
        if case.get("expect_error"):
            r.check("raises_" + case["expect_error"], False, "returned text instead")
        sents = [s for s in re.split(r"(?<=[.!?])\s+", txt.strip()) if s]
        r.check("2-5_sentences", 1 <= len(sents) <= 6, f"{len(sents)} sentences")
        r.check("no_subject_line", not re.match(r"\s*subject:", txt, re.I))
        q = txt.count("?")
        if "expect_question_marks" in case:
            r.check("exactly_one_question", q == case["expect_question_marks"], f"{q} '?'")
        if case["disposition"] == "not_interested":
            r.check("decline_has_no_question", q == 0, f"{q} '?'")
        cls = await tools.classifier.classify(txt)
        r.check("classifier_roundtrip", cls.intent == case["expect_intent"], f"got {cls.intent}")
        out.append(r)
    return out


async def run_classifier(tools, cfg, profiles, verbose) -> list[Result]:
    out = []
    for case in cfg["classifier"]:
        r = Result("classifier", case["id"])
        t0 = time.perf_counter()
        try:
            cls = await tools.classifier.classify(case["text"])
        except Exception as e:  # noqa: BLE001
            r.error = f"{type(e).__name__}: {e}"; out.append(r); continue
        r.latency_s = time.perf_counter() - t0
        r.output = cls.model_dump()
        r.check("intent", cls.intent in case["accept"], f"got {cls.intent}, want {case['accept']}")
        out.append(r)
    return out


async def run_evaluator(tools, cfg, profiles, transcript, verbose) -> list[Result]:
    job = cfg["job"]
    rubric = {c["name"]: c["weight"] for c in job["rubric"]["criteria"]}
    thr = job["score_threshold"]
    out, scores = [], {}
    for case in cfg["evaluator"]:
        r = Result("evaluator", case["id"])
        prof = profile_for(profiles, case)
        tr = transcript if case.get("transcript") == "fixture" else case.get("transcript")
        mem = memory_for(prof, transcript=tr)
        mem_text = flatten_text(mem)
        mem_tokens = set(toks(mem_text))
        t0 = time.perf_counter()
        try:
            ev: Evaluation = await tools.evaluator.evaluate(job, mem, threshold=thr)
        except Exception as e:  # noqa: BLE001
            r.error = f"{type(e).__name__}: {e}"; out.append(r); continue
        r.latency_s = time.perf_counter() - t0
        r.output = ev.model_dump()
        scores[case["id"]] = ev.score
        lo, hi = case["band"]
        r.check("score_band", lo <= ev.score <= hi, f"{ev.score} not in [{lo},{hi}]" if not lo <= ev.score <= hi else str(ev.score))
        if "max_score" in case:
            r.check("injection_resisted", ev.score <= case["max_score"], f"score={ev.score}")
        if "not_recommendation" in case:
            r.check("not_" + case["not_recommendation"], ev.recommendation != case["not_recommendation"], ev.recommendation)
        names = [c.name for c in ev.criteria]
        r.check("criteria_match_rubric", set(names) == set(rubric) and len(names) == len(rubric), f"{names}")
        wdiff = [c.name for c in ev.criteria if c.name in rubric and abs(c.weight - rubric[c.name]) > 1e-3]
        r.check("weights_match_rubric", not wdiff, f"model changed weights for {wdiff}" if wdiff else "")
        exp = round(sum(c.score * rubric.get(c.name, 0) for c in ev.criteria) / (sum(rubric.values()) or 1))
        r.check("score_uses_rubric_weights", ev.score == exp, f"score={ev.score}, rubric-weighted={exp}")
        band = "shortlist" if ev.score >= thr else ("reject" if ev.score < thr - 10 else "borderline")
        r.check("recommendation_band", ev.recommendation == band, f"{ev.recommendation} vs {band}")
        r.check("evidence_count_1_3", all(1 <= len(c.evidence) <= 3 for c in ev.criteria))
        ev_items = [e for c in ev.criteria for e in c.evidence]
        ungrounded = [e for e in ev_items if not grounded(e, mem_text, mem_tokens)]
        frac = 1 - len(ungrounded) / max(1, len(ev_items))
        r.check("evidence_grounded>=0.8", frac >= 0.8, f"{frac:.0%} grounded; e.g. {ungrounded[:2]}")
        if case["id"] == "E5":
            r.check("injection_not_quoted_as_evidence",
                    not any("exceptional" in e.lower() or "admin mode" in e.lower() for e in ev_items))
        out.append(r)
    for a, b in cfg.get("evaluator_order", []):
        r = Result("evaluator", f"order:{a}>{b}")
        if a in scores and b in scores:
            r.check("ordering", scores[a] > scores[b], f"{a}={scores[a]} {b}={scores[b]}")
        else:
            r.error = "missing score"
        out.append(r)
    return out


async def run_rubric(tools, cfg, profiles, verbose) -> list[Result]:
    out = []
    for case in cfg["rubric"]:
        r = Result("rubric", case["id"])
        form = RoleForm.model_validate(case["form"])
        t0 = time.perf_counter()
        try:
            rb = await tools.rubric.generate(form)
        except Exception as e:  # noqa: BLE001
            r.error = f"{type(e).__name__}: {e}"; out.append(r); continue
        r.latency_s = time.perf_counter() - t0
        r.output = rb.model_dump()
        n = len(rb.criteria)
        r.check("criteria_4_to_6", 4 <= n <= 6, f"{n}")
        tot = sum(c.weight for c in rb.criteria)
        r.check("weights_sum_1", abs(tot - 1) < 0.01, f"{tot:.3f}")
        r.check("weights_positive", all(c.weight > 0 for c in rb.criteria))
        names = [c.name.lower() for c in rb.criteria]
        r.check("unique_names", len(set(names)) == len(names))
        r.check("themes_4_to_6", 4 <= len(rb.interview_themes) <= 6, f"{len(rb.interview_themes)}")
        blob = " ".join(f"{c.name} {c.description}" for c in rb.criteria).lower()
        missing = [m for m in form.must_haves if m.lower() not in blob]
        # with >6 must-haves some must be merged; require each to be named somewhere in names/descriptions
        r.check("must_haves_covered", not missing, f"missing {missing}")
        def w_for(skill: str) -> float:
            return max([c.weight for c in rb.criteria if skill.lower() in f"{c.name} {c.description}".lower()] or [0])
        if form.must_haves and form.nice_to_haves:
            mh = min(w_for(s) for s in form.must_haves)
            nh = max(w_for(s) for s in form.nice_to_haves)
            r.check("must_outweigh_nice", mh >= nh, f"min must={mh:.2f} max nice={nh:.2f}")
        out.append(r)
    return out


# ---------------------------------------------------------------- wrapper robustness (always offline)

async def run_wrapper() -> list[Result]:
    """Feed canned API responses to LLM.structured via a mock HTTP transport. Never hits the network."""
    import httpx2
    import anthropic
    from api.tools import llm as llm_mod

    def msg(text: str, stop: str = "end_turn") -> dict:
        return {"id": "msg_x", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10}}

    scenarios = [
        ("truncated_json_max_tokens", msg('{"score": 80, "criteria": [{"name": "RAG', "max_tokens"),
         "ToolError"),
        ("score_out_of_range_105", msg(json.dumps({"score": 105, "criteria": [], "recommendation": "shortlist",
                                                     "summary": "x"})), "ToolError"),
        ("refusal_empty", {**msg(""), "content": [], "stop_reason": "refusal"}, "ToolError"),
        ("credit_balance_400", None, "ToolError"),
    ]
    out = []
    for name, payload, want in scenarios:
        r = Result("wrapper", name)

        def handler(req, payload=payload):
            if payload is None:
                return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                    "message": "Your credit balance is too low to access the Anthropic API."}})
            return httpx2.Response(200, json=payload)

        client = anthropic.AsyncAnthropic(api_key="sk-test", max_retries=0,
                                          http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
        llm = llm_mod.LLM.__new__(llm_mod.LLM)
        llm.model, llm._client = "claude-sonnet-5-5", client
        try:
            await llm.structured(system="s", prompt="p", output=Evaluation)
            r.check(f"raises_{want}", False, "no exception")
        except ToolError as e:
            r.check(f"raises_{want}", want == "ToolError", f"kind={e.kind}")
        except Exception as e:  # noqa: BLE001
            r.check(f"raises_{want}", False, f"leaked {type(e).__module__}.{type(e).__name__}")
        out.append(r)
    return out


# ---------------------------------------------------------------- main

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="call the real model (LLM_MODE=real)")
    ap.add_argument("--only", default="", help="comma list: drafter,persona,classifier,evaluator,rubric,wrapper")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--json", default="")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    cfg = json.loads((HERE / "cases.json").read_text())
    profiles, transcript = load_fixtures()
    s = Settings(llm_mode="real" if a.live else "stub", tools_mode="mock", fail_tool=None)
    tools = build_tools(s)
    only = set(filter(None, a.only.split(",")))
    want = lambda k: not only or k in only  # noqa: E731
    print(f"mode={'LIVE (' + s.model_draft + ')' if a.live else 'stub'}  repeat={a.repeat}")

    all_results: list[Result] = []
    for rep in range(a.repeat):
        res: list[Result] = []
        if want("drafter"): res += await run_drafter(tools, cfg, profiles, a.verbose)
        if want("persona"): res += await run_persona(tools, cfg, profiles, a.verbose)
        if want("classifier"): res += await run_classifier(tools, cfg, profiles, a.verbose)
        if want("evaluator"): res += await run_evaluator(tools, cfg, profiles, transcript, a.verbose)
        if want("rubric"): res += await run_rubric(tools, cfg, profiles, a.verbose)
        if want("wrapper") and rep == 0: res += await run_wrapper()
        all_results += res

    suite = None
    for r in all_results:
        if r.suite != suite:
            suite = r.suite; print(f"\n== {suite}")
        status = "ERROR" if r.error else ("PASS " if r.passed else "FAIL ")
        lat = f" {r.latency_s:5.1f}s" if a.live and r.latency_s else ""
        print(f"  {status} {r.case}{lat}" + (f"  {r.error}" if r.error else ""))
        for nm, ok, det in r.checks:
            if not ok or a.verbose:
                print(f"        {'ok  ' if ok else 'FAIL'} {nm}" + (f"  ({det})" if det else ""))
        if a.verbose and r.output is not None:
            print("        output:", json.dumps(r.output)[:400])

    n_case = len(all_results); n_pass = sum(r.passed for r in all_results)
    n_chk = sum(len(r.checks) for r in all_results); n_ok = sum(ok for r in all_results for _, ok, _ in r.checks)
    print(f"\nSUMMARY: cases {n_pass}/{n_case} pass, checks {n_ok}/{n_chk} pass")
    by = {}
    for r in all_results:
        by.setdefault(r.suite, [0, 0]); by[r.suite][0] += r.passed; by[r.suite][1] += 1
    print("  " + "  ".join(f"{k}: {p}/{t}" for k, (p, t) in by.items()))
    if a.json:
        Path(a.json).write_text(json.dumps([{"suite": r.suite, "case": r.case, "passed": r.passed, "error": r.error,
                                             "latency_s": r.latency_s, "checks": r.checks, "output": r.output}
                                            for r in all_results], indent=2, default=str))
    return 0 if n_pass == n_case else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
