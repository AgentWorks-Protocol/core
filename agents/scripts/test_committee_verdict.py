"""Unit test for reasoning.committee_verdict — the in-process M-of-N tally the ACP evaluator adapter returns.
Mocks evaluate_member + registry.evaluators so it needs no live LLM or CAW. Run:

    agents/.venv/Scripts/python.exe agents/scripts/test_committee_verdict.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reasoning
import registry


def _member(name: str):
    return SimpleNamespace(name=name, llm=lambda: {})


def _fake_members(names):
    def _patch():
        return [_member(n) for n in names]
    return _patch


def run() -> None:
    orig_member, orig_eval, orig_registry = reasoning.evaluate_member, reasoning.evaluate, registry.evaluators
    passed = 0

    def check(label, cond):
        nonlocal passed
        assert cond, f"FAIL: {label}"
        passed += 1
        print(f"  ok: {label}")

    try:
        # 3-member committee, quorum 2. Votes come from the member name suffix.
        votes = {}
        reasoning.evaluate_member = lambda spec, deliv, *, member_name, llm=None: {
            "accept": votes.get(member_name, False), "reason": f"{member_name} says {votes.get(member_name)}"}
        registry.evaluators = _fake_members(["Evaluator A", "Evaluator B", "Evaluator C"])

        # 2-of-3 approve -> accept
        votes = {"Evaluator A": True, "Evaluator B": True, "Evaluator C": False}
        v = reasoning.committee_verdict("spec", "deliverable")
        check("2/3 approve -> accept", v["accept"] is True and v["approve"] == 2 and v["reject"] == 1 and v["quorum"] == 2)
        check("2/3 reasons carry all members", len(v["reasons"]) == 3)

        # 1-of-3 approve -> reject
        votes = {"Evaluator A": True, "Evaluator B": False, "Evaluator C": False}
        v = reasoning.committee_verdict("spec", "deliverable")
        check("1/3 approve -> reject", v["accept"] is False and v["approve"] == 1)

        # 3-of-3 approve -> accept
        votes = {"Evaluator A": True, "Evaluator B": True, "Evaluator C": True}
        v = reasoning.committee_verdict("spec", "deliverable")
        check("3/3 approve -> accept", v["accept"] is True and v["approve"] == 3)

        # explicit quorum=3 makes a 2/3 fail
        votes = {"Evaluator A": True, "Evaluator B": True, "Evaluator C": False}
        v = reasoning.committee_verdict("spec", "deliverable", quorum=3)
        check("quorum=3 with 2/3 -> reject", v["accept"] is False and v["quorum"] == 3)

        # empty committee -> single-judge fallback (evaluate), quorum 1
        registry.evaluators = _fake_members([])
        reasoning.evaluate = lambda spec, deliv: {"accept": True, "reason": "solo yes"}
        v = reasoning.committee_verdict("spec", "deliverable")
        check("empty committee -> single-judge fallback accept", v["accept"] is True and v["quorum"] == 1 and len(v["reasons"]) == 1)

        # 1-member committee with default quorum 2 -> clamped to 1
        registry.evaluators = _fake_members(["Evaluator A"])
        votes = {"Evaluator A": True}
        reasoning.evaluate_member = lambda spec, deliv, *, member_name, llm=None: {"accept": votes.get(member_name, False), "reason": "x"}
        v = reasoning.committee_verdict("spec", "deliverable")
        check("1-member clamps quorum to 1 -> accept", v["accept"] is True and v["quorum"] == 1)
    finally:
        reasoning.evaluate_member, reasoning.evaluate, registry.evaluators = orig_member, orig_eval, orig_registry

    print(f"\nALL {passed} CHECKS PASSED")


if __name__ == "__main__":
    run()
