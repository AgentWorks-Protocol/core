"""Genuine agent reasoning layer (DeepSeek via the OpenAI-compatible API).

The agents call these to actually DECIDE at the economic decision points (criterion 1):
  - client_decide_fund: is this task worth funding within policy/budget?
  - provider_do_task:    actually perform the task (produce the deliverable).
  - evaluate:            the real accept/reject judgment on the deliverable.

CAW Pacts remain the hard boundary regardless of what the LLM decides - that's the safety story.
Every call is logged (decision + rationale) so the demo's audit trail is legible.
"""

from __future__ import annotations

import json
import logging

from openai import OpenAI

import config

log = logging.getLogger("reason")

# One OpenAI-compatible client per (api_key, base_url). Committee members each reason on a DISTINCT
# provider (e.g. DeepSeek / Groq / Gemini via their OpenAI-compatible endpoints), so a quorum is a
# genuine M-of-N of independent models — not one model voting N times. Default (api_key/base_url None)
# is the global config.LLM_* (DeepSeek).
_clients: dict[tuple[str, str], OpenAI] = {}


def _client_for(api_key: str | None = None, base_url: str | None = None) -> OpenAI:
    ak = api_key or config.LLM_API_KEY
    bu = base_url or config.LLM_BASE_URL
    if not ak:
        raise RuntimeError("LLM api key is empty - set LLM_API_KEY (or a per-member CAW_EVALUATOR_n_LLM_API_KEY)")
    cache_key = (ak, bu)
    if cache_key not in _clients:
        _clients[cache_key] = OpenAI(api_key=ak, base_url=bu)
    return _clients[cache_key]


def _llm() -> OpenAI:  # back-compat: the default client
    return _client_for()


# DeepSeek's deepseek-v4-flash is a REASONING model: chain-of-thought goes to reasoning_content
# and consumes completion tokens, so budgets must be generous or `content` comes back empty.
def _chat(system: str, user: str, *, json_mode: bool = False, max_tokens: int = 2000,
          api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> str:
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _client_for(api_key, base_url).chat.completions.create(
        model=model or config.LLM_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=max_tokens,
        **kwargs,
    )
    return resp.choices[0].message.content or ""


def _json(system: str, user: str, *, api_key: str | None = None, base_url: str | None = None,
          model: str | None = None) -> dict:
    raw = _chat(system, user, json_mode=True, api_key=api_key, base_url=base_url, model=model)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        return json.loads(raw[start : end + 1])


def smoke() -> str:
    """One-call check that the endpoint + model id work. Returns the model's reply."""
    out = _chat("You are a terse assistant.", "Reply with exactly: OK", max_tokens=200)
    log.info("[reason] smoke model=%s base=%s -> %r", config.LLM_MODEL, config.LLM_BASE_URL, out)
    return out


# ── decision points ──

def client_decide_fund(task: str, price_usdc: float, budget_usdc: float) -> dict:
    system = (
        "You are the Client agent in a trustless escrow marketplace. Decide whether to fund a job. "
        "Consider whether the task is legitimate and the price is reasonable and within budget. "
        'Respond ONLY as JSON: {"fund": true|false, "reason": "<one sentence>"}.'
    )
    user = f"Task: {task}\nPrice: {price_usdc} USDC\nRemaining budget: {budget_usdc} USDC"
    d = _json(system, user)
    log.info("[reason] client_decide_fund -> %s", d)
    return d


def provider_decide_accept(spec: str, reward_usdc: float, *, provider_name: str = "Provider") -> dict:
    """Provider genuinely decides whether to CLAIM an open, funded job (criterion 1).

    In the open marketplace multiple providers see the same job; each reasons independently about
    whether the reward justifies the work. The on-chain acceptJob race then settles who gets it.
    """
    system = (
        "You are a Provider agent in an open, trustless escrow marketplace. An OPEN job has been "
        "funded and any provider may claim it by accepting on-chain. Decide whether YOU should accept "
        "this job: is the task something you can deliver well, and is the reward worth the effort? "
        'Respond ONLY as JSON: {"accept": true|false, "reason": "<one sentence>"}.'
    )
    user = f"Provider: {provider_name}\nTask: {spec}\nReward: {reward_usdc} USDC"
    d = _json(system, user)
    log.info("[reason] provider_decide_accept(%s) -> %s", provider_name, d)
    return d


def provider_do_task(spec: str, *, sabotage: bool = False) -> str:
    if sabotage:
        # Deliberately produce an off-spec deliverable so the evaluator genuinely rejects it.
        system = "You are a lazy contractor. Produce a deliverable that does NOT satisfy the request (wrong topic / wrong form)."
    else:
        system = "You are the Provider agent. Complete the requested task well and concisely. Output only the deliverable."
    out = _chat(system, spec, max_tokens=2000)
    log.info("[reason] provider_do_task(sabotage=%s) -> %r", sabotage, out[:120])
    return out.strip()


def evaluate(spec: str, deliverable: str) -> dict:
    system = (
        "You are the Evaluator in an escrow marketplace. Judge whether the deliverable satisfies the task spec. "
        "Be fair but strict: accept only if it genuinely meets the request. "
        'Respond ONLY as JSON: {"accept": true|false, "reason": "<one sentence>"}.'
    )
    user = f"TASK SPEC:\n{spec}\n\nDELIVERABLE:\n{deliverable}"
    d = _json(system, user)
    log.info("[reason] evaluate -> %s", d)
    return d


# Distinct evaluator personas so committee members reason independently (criterion 1) rather than
# echoing one another — the consensus is meaningful only if the votes are genuinely arrived at.
_EVAL_PERSONAS = {
    "Evaluator A": "You weigh CORRECTNESS first: does the deliverable factually + technically satisfy the spec?",
    "Evaluator B": "You weigh COMPLETENESS + FORM first: does it meet every stated acceptance criterion and format?",
    "Evaluator C": "You weigh CLARITY + USEFULNESS first: would the requester actually be satisfied receiving this?",
}


def evaluate_member(spec: str, deliverable: str, *, member_name: str = "Evaluator",
                    llm: dict | None = None) -> dict:
    """One committee member's independent accept/reject judgment. Independence is on two axes: a distinct
    reasoning LENS (persona) AND a distinct MODEL (`llm` = {api_key, base_url, model}; falls back to the
    global default). Returns {accept, reason} like {evaluate}."""
    lens = _EVAL_PERSONAS.get(member_name, "You judge whether the deliverable genuinely satisfies the spec.")
    system = (
        f"You are {member_name}, one member of an independent evaluator COMMITTEE in a trustless escrow "
        f"marketplace. {lens} Be fair but strict; vote your own honest assessment regardless of how other "
        'members might vote. Respond ONLY as JSON: {"accept": true|false, "reason": "<one sentence>"}.'
    )
    user = f"TASK SPEC:\n{spec}\n\nDELIVERABLE:\n{deliverable}"
    llm = llm or {}
    d = _json(system, user, api_key=llm.get("api_key"), base_url=llm.get("base_url"), model=llm.get("model"))
    log.info("[reason] evaluate_member(%s, model=%s) -> %s", member_name, llm.get("model") or config.LLM_MODEL, d)
    return d


def committee_verdict(spec: str, deliverable: str, *, members=None, quorum: int | None = None) -> dict:
    """Run the whole M-of-N committee IN-PROCESS and tally to quorum — the pure-reasoning verdict an ACP
    evaluator adapter returns. Model A never touches our on-chain escrow, so aggregation lives here, mirroring
    the contract's `castVote` `count >= quorum` rule (AgentWorksEscrowV4.sol). Each member judges independently
    (own persona + own model via `evaluate_member`).

    Returns {accept, approve, reject, quorum, reasons: [{member, accept, reason}]}. An empty committee falls
    back to the single-judge `evaluate` so a degenerate 0-member config still yields a verdict (no divide-by-zero).
    """
    import registry  # lazy import: registry only needs config (already loaded) — avoids any import-time cycle
    members = members if members is not None else registry.evaluators()
    if not members:
        d = evaluate(spec, deliverable)
        acc = bool(d.get("accept"))
        return {"accept": acc, "approve": int(acc), "reject": int(not acc), "quorum": 1,
                "reasons": [{"member": "Evaluator", "accept": acc, "reason": d.get("reason", "")}]}
    q = min(max(1, quorum or config.COMMITTEE_QUORUM), len(members))  # clamp to [1, N]
    reasons, approve = [], 0
    for m in members:
        v = evaluate_member(spec, deliverable, member_name=m.name, llm=m.llm())
        acc = bool(v.get("accept"))
        reasons.append({"member": m.name, "accept": acc, "reason": v.get("reason", "")})
        approve += 1 if acc else 0
    verdict = {"accept": approve >= q, "approve": approve, "reject": len(members) - approve,
               "quorum": q, "reasons": reasons}
    log.info("[reason] committee_verdict -> accept=%s (%s/%s of %s)", verdict["accept"], approve, q, len(members))
    return verdict
