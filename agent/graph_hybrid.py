"""The LangGraph agent.

Boundaries (the assessment reads boundaries rather than counting nodes):

* **route** -- LM classifier with a deterministic prior as fallback. Merged with nothing:
  it is the one decision that changes which downstream nodes run at all, so it is worth
  isolating and tracing on its own.
* **retrieve** -- BM25 top-k, then conflict expansion, then the injection quarantine.
  These three are one node because they are one transformation of the same object (the
  chunk list) and because a partially-sanitised chunk list must never be observable by a
  later node. The trace records all three sub-results separately.
* **plan** -- deterministic constraint extraction. Separate from retrieval because it is
  where conflicts become explicit, and separate from NL-to-SQL because its output is the
  contract between documents and SQL.
* **gate** -- decides answer-vs-escalate *before* spending LM calls. An unresolved
  conflict or an undocumented missing column cannot be fixed by better SQL, so
  discovering that after three model calls would be wasted work.
* **nl2sql -> execute -> synthesize -> validate** -- the answering path. Synthesis builds
  the typed answer, citations and confidence in code from the executed rows, and calls
  the LM only for the explanation, whose wording the determinism contract exempts.
* **repair** -- re-enters nl2sql with the specific failure as feedback, capped at 2.
* **review** -- terminal escalation with a packet.

Every node is wrapped in a traced step, so `traces/<id>.jsonl` is a replayable account
including each repair attempt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from agent import config
from agent.answer import AnswerError, build_answer, build_answer_from_text, score_confidence
from agent.datasets import constraints_text
from agent.injection import Quarantined, sanitize, wrap_untrusted
from agent.modules import DocAnswer, Explainer, NL2SQL, Router
from agent.planner import Plan, build_plan
from agent.retriever import Hit, Retriever
from agent.schema import all_columns, known_tables, schema_text
from agent.sql_analysis import physical_tables, schema_errors, unwrapped_date_comparisons
from agent.trace import Tracer
from agent.validator import Failure, validate
from models import OutputRecord, QuestionRecord, ReviewPacket
from sqlite_tool import SQLiteTool


class State(TypedDict, total=False):
    id: str
    question: str
    format_hint: str
    route: str
    router_detail: dict
    hits: list[Hit]
    quarantined: list[dict]
    plan: Plan
    constraints: str
    sql: str
    static_errors: list[str]
    columns: list[str]
    rows: list[tuple]
    exec_error: str | None
    final_answer: Any
    citations: list[str]
    assumptions: list[str]
    explanation: str
    confidence: float | None
    repairs: int
    failures: list[str]
    feedback: str
    status: str
    review_packet: dict | None
    blocker: str


@dataclass
class Deps:
    """Everything the graph needs, injected so tests can supply fakes."""
    retriever: Retriever
    tool: SQLiteTool
    nl2sql: NL2SQL
    doc_answer: DocAnswer
    explainer: Explainer
    router: Router
    db_schema: str
    schema_map: dict
    tables: list[str]
    columns: set[str]
    corpus_chunk_ids: set[str]
    baseline_artifact: bool = False
    traces_dir: Path = field(default_factory=lambda: config.TRACES_DIR)


def build_deps(db_path: Path | None = None, baseline_artifact: bool = False) -> Deps:
    db = str(db_path or config.DB_PATH)
    retr = Retriever(config.DOCS_DIR)
    return Deps(
        retriever=retr,
        tool=SQLiteTool(db, row_limit=config.SQL_ROW_LIMIT, timeout_s=config.SQL_TIMEOUT_S),
        nl2sql=NL2SQL(), doc_answer=DocAnswer(), explainer=Explainer(), router=Router(),
        db_schema=schema_text(db), schema_map=SQLiteTool(db).schema(),
        tables=known_tables(db), columns=all_columns(db),
        corpus_chunk_ids={c.id for c in retr.chunks},
        baseline_artifact=baseline_artifact,
    )


# --------------------------------------------------------------------------- nodes

def make_graph(deps: Deps, tracer: Tracer):
    def n_route(state: State) -> State:
        with tracer.step("routing", {"question": state["question"]}) as st:
            pred = deps.router(question=state["question"])
            detail = {"route": pred.route, "lm_route": pred.lm_route,
                      "rule_route": pred.rule_route, "used_fallback": pred.used_fallback}
            st.record(**detail)
            return {"route": pred.route, "router_detail": detail}

    def n_retrieve(state: State) -> State:
        with tracer.step("retrieval", {"question": state["question"], "k": config.RETRIEVE_K}) as st:
            top, extra = deps.retriever.retrieve(state["question"], config.RETRIEVE_K)
            cleaned: list[Hit] = []
            quarantined: list[dict] = []
            for h in top + extra:
                text, hits = sanitize(h.chunk_id, h.content)
                cleaned.append(Hit(h.chunk_id, h.score, h.source, text))
                quarantined += [{"chunk_id": q.chunk_id, "line": q.line, "rules": list(q.rules)}
                                for q in hits]
            st.record(
                top_k=[{"chunk_id": h.chunk_id, "score": h.score} for h in top],
                conflict_expansion=[h.chunk_id for h in extra],
                quarantined=quarantined,
            )
            return {"hits": cleaned, "quarantined": quarantined}

    def n_plan(state: State) -> State:
        with tracer.step("planning", {"chunks": [h.chunk_id for h in state["hits"]]}) as st:
            plan = build_plan(state["question"], state["hits"], deps.columns)
            text = constraints_text(plan)
            st.record(
                date_window=(f"{plan.chosen_window.start}..{plan.chosen_window.end}"
                             if plan.chosen_window else None),
                conflicts=[{"subject": c.subject, "kind": c.kind, "options": list(c.options),
                            "resolution": c.resolution, "resolved_by": c.resolved_by}
                           for c in plan.conflicts],
                missing_fields=[list(t) for t in plan.missing_fields],
                blocking_missing=[list(t) for t in plan.blocking_missing_fields],
                reporting_groups={k: list(v) for k, v in plan.reporting_groups.items()},
                assumptions=plan.assumptions,
                constraints=text,
            )
            return {"plan": plan, "constraints": text, "assumptions": list(plan.assumptions)}

    def n_gate(state: State) -> State:
        """Escalate before spending LM calls on something SQL cannot fix."""
        plan: Plan = state["plan"]
        with tracer.step("review_gate", {"route": state["route"]}) as st:
            blocker = ""
            if plan.unresolved_conflicts:
                c = plan.unresolved_conflicts[0]
                blocker = (f"The corpus gives conflicting values for {c.subject} and no "
                           f"precedence rule: {' vs '.join(c.options)}.")
            elif plan.blocking_missing_fields:
                fname, formula, src = plan.blocking_missing_fields[0]
                blocker = (f"The {formula} formula in {src} needs a {fname} column that does "
                           f"not exist in the database, and no approximation is documented "
                           f"anywhere in the corpus or supplied by the question.")
            st.record(blocked=bool(blocker), blocker=blocker or None)
            return {"blocker": blocker}

    def n_nl2sql(state: State) -> State:
        attempt = state.get("repairs", 0)
        with tracer.step("nl2sql", {"question": state["question"],
                                    "feedback": state.get("feedback", "")}, attempt) as st:
            pred = deps.nl2sql(question=state["question"], format_hint=state["format_hint"],
                               db_schema=deps.db_schema, constraints=state["constraints"],
                               feedback=state.get("feedback", ""))
            lint = unwrapped_date_comparisons(pred.sql)
            # Static check before execution. A hallucinated column or an undefined alias
            # is diagnosable from the schema alone, and saying exactly which column is
            # wrong converts a wasted attempt into a targeted correction.
            static = schema_errors(pred.sql, deps.schema_map) if pred.sql else []
            st.record(sql=pred.sql, raw=pred.raw_sql, date_lint=lint, static_errors=static)
            return {"sql": pred.sql, "static_errors": static}

    def n_execute(state: State) -> State:
        attempt = state.get("repairs", 0)
        with tracer.step("execution", {"sql": state.get("sql", "")}, attempt) as st:
            sql = state.get("sql", "")
            if not sql:
                st.record(error="no SQL produced", row_count=0)
                return {"columns": [], "rows": [], "exec_error": "no SQL produced"}
            res = deps.tool.execute(sql)
            st.record(columns=res.columns, row_count=res.row_count,
                      rows=[list(r) for r in res.rows], error=res.error,
                      truncated=res.truncated, elapsed_ms=res.elapsed_ms)
            return {"columns": res.columns, "rows": list(res.rows), "exec_error": res.error}

    def n_rag_answer(state: State) -> State:
        attempt = state.get("repairs", 0)
        blocks = [(h.chunk_id, h.content) for h in state["hits"]]
        with tracer.step("synthesis", {"route": "rag", "chunks": [b[0] for b in blocks]}, attempt) as st:
            pred = deps.doc_answer(question=state["question"], format_hint=state["format_hint"],
                                   context=wrap_untrusted(blocks))
            st.record(extracted=pred.value, insufficient=pred.insufficient)
            if pred.insufficient:
                return {"final_answer": None, "sql": "",
                        "blocker": ("The documents do not contain a single value for this "
                                    "question; the relevant policy is stated as a range or "
                                    "excludes the case asked about.")}
            try:
                answer = build_answer_from_text(state["format_hint"], pred.value)
            except AnswerError as e:
                st.record(coercion_error=str(e))
                return {"final_answer": None, "sql": "",
                        "failures": [f"format: {e}"]}
            used = _chunks_supporting(state, answer)
            return {"final_answer": answer, "sql": "", "citations": used}

    def n_synthesize(state: State) -> State:
        attempt = state.get("repairs", 0)
        with tracer.step("synthesis", {"route": state["route"], "sql": state.get("sql", "")}, attempt) as st:
            rows, cols = state.get("rows", []), state.get("columns", [])
            if state.get("exec_error"):
                st.record(skipped="execution failed")
                return {"final_answer": None, "failures": [f"execution: {state['exec_error']}"]}
            try:
                answer = build_answer(state["format_hint"], cols, rows)
            except AnswerError as e:
                st.record(coercion_error=str(e))
                return {"final_answer": None, "failures": [f"format: {e}"]}
            tables = physical_tables(state["sql"], deps.tables)
            chunks = _chunks_supporting(state, answer)
            st.record(answer=answer, tables=tables, chunks=chunks)
            return {"final_answer": answer, "citations": tables + chunks}

    def n_validate(state: State) -> State:
        attempt = state.get("repairs", 0)
        with tracer.step("validation", {"citations": state.get("citations", [])}, attempt) as st:
            if state.get("final_answer") is None:
                fails = state.get("failures") or ["format: no answer produced"]
                st.record(failures=fails)
                return {"failures": fails}
            seen = {h.chunk_id for h in state["hits"]}
            fails = validate(
                final_answer=state["final_answer"], format_hint=state["format_hint"],
                sql=state.get("sql", ""), citations=state.get("citations", []),
                columns=state.get("columns", []), rows=state.get("rows", []),
                known_tables=deps.tables, corpus_chunk_ids=deps.corpus_chunk_ids,
                seen_chunk_ids=seen, route=state["route"],
            )
            st.record(failures=[str(f) for f in fails], ok=not fails)
            return {"failures": [str(f) for f in fails]}

    def n_repair(state: State) -> State:
        n = state.get("repairs", 0) + 1
        problems = list(state.get("static_errors") or [])
        problems += state.get("failures") or []
        if state.get("exec_error"):
            problems = [f"execution: {state['exec_error']}"] + problems
        lint = unwrapped_date_comparisons(state.get("sql", ""))
        if lint:
            problems.append(
                f"the columns {lint} were compared without date(); OrderDate mixes "
                f"'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS', so wrap it as date(col)")
        if not state.get("rows") and not state.get("exec_error"):
            problems.append("the query returned no rows; check the filters and the date window")
        feedback = ("Previous SQL:\n" + (state.get("sql") or "(none)") +
                    "\nProblems:\n" + "\n".join(f"- {p}" for p in problems))
        with tracer.step("repair", {"attempt": n, "problems": problems}, n) as st:
            st.record(feedback=feedback)
        return {"repairs": n, "feedback": feedback, "failures": [],
                "exec_error": None, "citations": [], "static_errors": []}

    def n_review(state: State) -> State:
        with tracer.step("review_gate", {"final": True}) as st:
            considered = [state.get("sql") or "(no SQL produced)"]
            considered += [h.chunk_id for h in state.get("hits", [])]
            blocker = state.get("blocker") or "; ".join(state.get("failures") or []) or \
                "The agent could not produce an answer that satisfies the output contract."
            packet = ReviewPacket(
                question=state["question"],
                understood=_understood(state),
                blocker=blocker[:400],
                considered=considered[:6],
                decision_needed=_decision_needed(state),
            )
            st.record(review_packet=packet.model_dump())
            return {"status": "needs_review", "review_packet": packet.model_dump(),
                    "confidence": None, "final_answer": None}

    def n_finish(state: State) -> State:
        plan: Plan = state.get("plan") or Plan()
        with tracer.step("synthesis", {"stage": "explain"}) as st:
            evidence = (f"SQL: {state.get('sql') or '(none)'}\n"
                        f"rows: {len(state.get('rows') or [])}\n"
                        f"constraints: {state.get('constraints', '')[:400]}")
            if config.USE_LM_EXPLANATION:
                try:
                    explanation = deps.explainer(question=state["question"],
                                                 evidence=evidence).explanation
                except Exception as e:                   # prose is never load-bearing
                    explanation = describe(state, plan)
                    st.record(explainer_error=str(e))
            else:
                explanation = describe(state, plan)
            resolved = [c for c in plan.conflicts if c.resolution]
            conf = score_confidence(
                route=state["route"], repairs=state.get("repairs", 0),
                rows=len(state.get("rows") or []),
                conflicts_resolved=len(resolved),
                invented_approximation=bool(plan.blocking_missing_fields),
                supplied_approximation=bool(plan.supplied_approximations
                                            and plan.missing_fields),
                doc_precedence_applied=any(c.resolved_by == "document-precedence" for c in resolved),
                legacy_window_ambiguity=_legacy_ambiguity(state, plan),
                used_fallback_route=bool(state.get("router_detail", {}).get("used_fallback")),
                baseline_artifact=deps.baseline_artifact,
            )
            assumptions = _assumptions(state, plan)
            st.record(confidence=conf.value, rubric=conf.notes, assumptions=assumptions)
            return {"status": "answered", "explanation": explanation,
                    "confidence": conf.value, "assumptions": assumptions}

    # ----------------------------------------------------------------- edges
    def after_gate(state: State) -> Literal["review", "rag", "sql"]:
        if state.get("blocker"):
            return "review"
        return "rag" if state["route"] == "rag" else "sql"

    def after_nl2sql(state: State) -> Literal["execute", "repair"]:
        """Skip execution when the SQL provably cannot run, unless repairs are spent.

        At the cap we execute anyway: the real executor error belongs in the trace, and
        the static checker is a heuristic that must never be the last word.
        """
        if state.get("static_errors") and state.get("repairs", 0) < config.MAX_REPAIRS:
            return "repair"
        return "execute"

    def after_rag(state: State) -> Literal["review", "validate"]:
        return "review" if state.get("blocker") else "validate"

    def after_validate(state: State) -> Literal["finish", "repair", "review"]:
        if not state.get("failures"):
            return "finish"
        if state.get("repairs", 0) >= config.MAX_REPAIRS:
            return "review"
        return "repair"

    g = StateGraph(State)
    for name, fn in [("route", n_route), ("retrieve", n_retrieve), ("plan", n_plan),
                     ("gate", n_gate), ("nl2sql", n_nl2sql), ("execute", n_execute),
                     ("rag_answer", n_rag_answer), ("synthesize", n_synthesize),
                     ("validate", n_validate), ("repair", n_repair),
                     ("review", n_review), ("finish", n_finish)]:
        g.add_node(name, fn)

    g.add_edge(START, "route")
    g.add_edge("route", "retrieve")
    g.add_edge("retrieve", "plan")
    g.add_edge("plan", "gate")
    g.add_conditional_edges("gate", after_gate,
                            {"review": "review", "rag": "rag_answer", "sql": "nl2sql"})
    g.add_conditional_edges("rag_answer", after_rag,
                            {"review": "review", "validate": "validate"})
    g.add_conditional_edges("nl2sql", after_nl2sql,
                            {"execute": "execute", "repair": "repair"})
    g.add_edge("execute", "synthesize")
    g.add_edge("synthesize", "validate")
    g.add_conditional_edges("validate", after_validate,
                            {"finish": "finish", "repair": "repair", "review": "review"})
    g.add_edge("repair", "nl2sql")
    g.add_edge("finish", END)
    g.add_edge("review", END)
    return g.compile()


# --------------------------------------------------------------------------- helpers

def describe(state: State, plan: Plan) -> str:
    """Deterministic explanation, at most two sentences.

    Built from what the graph actually did, so it is accurate by construction rather than
    by the model's recollection: the route, the resolved window, the formula applied, the
    tables read and the number of rows behind the figure.
    """
    if state["route"] == "rag":
        chunks = [c for c in state.get("citations", []) if "::" in c]
        return ("Read directly from " + (", ".join(chunks) or "the retrieved policy text")
                + "; no database query was needed.")
    bits: list[str] = []
    if plan.chosen_window:
        bits.append(f"restricted to {plan.chosen_window.start}..{plan.chosen_window.end}")
    names = sorted({k.name for k in plan.kpi_formulas})
    if names:
        bits.append("applying the " + "/".join(names) + " definition from the KPI docs")
    for group in plan.reporting_groups:
        if _mentions_reporting_group(state, plan):
            bits.append(f"rolled up into the '{group}' reporting group")
    tables = [c for c in state.get("citations", []) if "::" not in c]
    first = ("Queried " + (", ".join(tables) or "the database")
             + (" " + " and ".join(bits) if bits else "") + ".")
    second = f"The figure comes from {len(state.get('rows') or [])} returned row(s)"
    if state.get("repairs"):
        second += f" after {state['repairs']} repair attempt(s)"
    return first + " " + second + "."


def _chunks_supporting(state: State, answer: Any) -> list[str]:
    """Chunks the answer actually rested on.

    Only chunks whose content shaped the plan are cited: the resolved date window's
    source, the KPI formula's source, a reporting group's source, and for a document
    lookup the chunk the value came from. Citing everything retrieved would be an
    invented citation, which the contract penalises.
    """
    plan: Plan = state.get("plan") or Plan()
    used: list[str] = []

    def add(cid: str | None) -> None:
        if cid and cid not in used:
            used.append(cid)

    if plan.chosen_window:
        add(plan.chosen_window.source)
        for c in plan.conflicts:
            if c.kind == "date_window" and c.resolution:
                for opt in c.options:                     # both sides were read
                    if "(" in opt and opt.rstrip(")").rsplit("(", 1)[-1]:
                        add(opt.rstrip(")").rsplit("(", 1)[-1])
    for k in plan.kpi_formulas:
        if _formula_relevant(state, k.name):
            add(k.source)
    if plan.reporting_groups and _mentions_reporting_group(state, plan):
        for h in state.get("hits", []):
            if any(g in h.content for g in plan.reporting_groups):
                add(h.chunk_id)
    if state["route"] == "rag":
        add(_best_policy_chunk(state, answer))
    return used


def _formula_relevant(state: State, name: str) -> bool:
    q = state["question"].lower()
    n = name.lower()
    if n in ("aov",):
        return "aov" in q or "average order value" in q
    if n in ("gm",):
        return "margin" in q
    if n == "revenue":
        return "revenue" in q
    return n in q


def _mentions_reporting_group(state: State, plan: Plan) -> bool:
    q = state["question"].lower()
    return "reporting group" in q or any(g.lower() in q for g in plan.reporting_groups)


def _best_policy_chunk(state: State, answer: Any) -> str | None:
    """For a document lookup, the chunk whose text contains the answer value."""
    needle = str(answer)
    for h in state.get("hits", []):
        if needle and needle in h.content:
            return h.chunk_id
    hits = state.get("hits") or []
    return hits[0].chunk_id if hits else None


def _legacy_ambiguity(state: State, plan: Plan) -> bool:
    """A KPI with an effective date, applied to a window that starts before it.

    Only reachable because the database really does hold pre-2016 orders; see
    DECISIONS.md.
    """
    w = plan.chosen_window
    starts = [w.start] if w else []
    import re
    starts += re.findall(r"\b(20\d{2})\b", state["question"])
    for k in plan.kpi_formulas:
        if k.effective and _formula_relevant(state, k.name):
            for s in starts:
                year = s[:4]
                if year and year < k.effective[:4]:
                    return True
    return False


def _assumptions(state: State, plan: Plan) -> list[str]:
    out = list(state.get("assumptions") or [])
    if state.get("quarantined"):
        lines = {q["chunk_id"] for q in state["quarantined"]}
        out.append(
            "Ignored instruction-like text in " + ", ".join(sorted(lines)) +
            " that directed a fixed answer; used the stated policy values instead.")
    for fname, formula, src in plan.missing_fields:
        if supplied := plan.supplied_approximations.get(fname):
            out.append(f"{fname} is not a column in this database; used the approximation "
                       f"given in the question ({supplied}).")
    if _legacy_ambiguity(state, plan):
        out.append("The window begins before the current KPI definition's effective date; "
                   "used the current definition because the question did not ask for the "
                   "legacy figure.")
    if state.get("repairs"):
        out.append(f"Required {state['repairs']} SQL repair attempt(s); "
                   "the final query is the one reported.")
    return out


def _understood(state: State) -> str:
    parts = [f"route={state.get('route', '?')}"]
    plan: Plan = state.get("plan") or Plan()
    if plan.chosen_window:
        parts.append(f"window {plan.chosen_window.start}..{plan.chosen_window.end}")
    if plan.kpi_formulas:
        parts.append("formulas " + ", ".join(sorted({k.name for k in plan.kpi_formulas})))
    parts.append(f"expected shape {state.get('format_hint')}")
    return "; ".join(parts)[:300]


def _decision_needed(state: State) -> str:
    plan: Plan = state.get("plan") or Plan()
    if plan.unresolved_conflicts:
        return ("Confirm which document is authoritative for this figure, then re-run. "
                "The corpus states no precedence between them.")
    if plan.blocking_missing_fields:
        fname = plan.blocking_missing_fields[0][0]
        return (f"Supply the {fname} proxy to use (for example a fixed percentage of the "
                f"line-item unit price), or point to a table that holds it.")
    return ("Confirm the intended interpretation, or supply the expected SQL, so the "
            "query can be corrected.")


# --------------------------------------------------------------------------- entry

def answer_question(q: QuestionRecord, deps: Deps, seed: int = 0) -> OutputRecord:
    tracer = Tracer(q.id, deps.traces_dir)
    graph = make_graph(deps, tracer)
    final = graph.invoke({"id": q.id, "question": q.question,
                          "format_hint": q.format_hint, "repairs": 0,
                          "assumptions": [], "citations": [], "failures": []})
    if final.get("status") == "answered":
        return OutputRecord(
            id=q.id, status="answered", final_answer=final["final_answer"],
            sql=final.get("sql") or "", confidence=final.get("confidence"),
            explanation=final.get("explanation", ""),
            assumptions=final.get("assumptions", []), repairs=final.get("repairs", 0),
            citations=final.get("citations", []), review_packet=None,
        )
    return OutputRecord(
        id=q.id, status="needs_review", final_answer=None,
        sql=final.get("sql") or "", confidence=None,
        explanation=final.get("explanation", "Escalated to a human reviewer."),
        assumptions=final.get("assumptions", []), repairs=final.get("repairs", 0),
        citations=final.get("citations", []),
        review_packet=ReviewPacket(**final["review_packet"]),
    )
