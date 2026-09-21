"""Bounded, explicit JEV candidate judgments; no implicit question dependencies."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from .skills import Plan


class InvalidJudgment(ValueError):
    """Malformed or out-of-domain answers must not authorize an action."""


def _number(value, maximum: float = 1.0) -> float:
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or not 0 <= value <= maximum):
        raise InvalidJudgment("Expected a finite in-range number")
    return float(value)


def validate_answers(questions: dict, answers: dict) -> None:
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise InvalidJudgment("Missing or unexpected answers")
    for key, question in questions.items():
        answer = answers[key]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise InvalidJudgment("Answer type mismatch")
        if kind == "noul":
            _number(answer.get("noul"))
            continue
        _number(answer.get("confidence"))
        probabilities = answer.get("probabilities")
        labels = (set(question["criteria"]) if kind == "choice"
                  else {str(i) for i in range(len(question["criteria"]))})
        if not isinstance(probabilities, dict) or set(probabilities) != labels:
            raise InvalidJudgment("Incomplete answer distribution")
        total = sum(_number(p) for p in probabilities.values())
        if not math.isclose(total, 1, abs_tol=1e-3):
            raise InvalidJudgment("Probabilities must sum to one")
        if kind == "choice":
            choice = answer.get("choice")
            if (not isinstance(choice, str) or choice not in labels
                    or probabilities[choice] + 1e-6 < max(probabilities.values())):
                raise InvalidJudgment("Choice must be an offered maximum-probability label")
        else:
            score = _number(answer.get("score"), len(labels) - 1)
            if answer.get("legend") != {str(i): level for i, level in enumerate(question["criteria"])}:
                raise InvalidJudgment("Score legend does not match the supplied rubric")
            expected = sum(int(key) * value for key, value in probabilities.items())
            if not math.isclose(score, expected, abs_tol=0.02):
                raise InvalidJudgment("Score conflicts with its probability distribution")


@dataclass
class Decision:
    plan_id: str | None
    source: str
    reason: str = ""
    state: dict = field(default_factory=dict)
    questions: dict = field(default_factory=dict)
    answers: dict = field(default_factory=dict)
    utilities: dict[str, float] = field(default_factory=dict)
    model_called: bool = False


def question_batch(state: dict, plans: list[Plan], max_bytes: int = 32000,
                   max_candidates: int = 16) -> tuple[dict, dict, list[Plan]]:
    """Bound serialized request bytes, NOT estimated tokens or provider limits."""
    if max_bytes < 1 or not 1 <= max_candidates <= 254:
        raise ValueError("Invalid request budget")
    selected = plans[:max_candidates]
    if len({p.id for p in selected}) != len(selected):
        raise ValueError("Duplicate candidate IDs")
    while selected:
        context = {**state, "candidate_plans": {p.id: p.to_dict() for p in selected}}
        questions = {
            "candidate": {
                "type": "choice",
                "instructions": ("Choose the best supplied candidate plan for `active_goal` using "
                                 "`facts` and `history`. Select observe when more evidence is needed. "
                                 "Do not assume other questions' answers are available."),
                "criteria": {**{p.id: p.description for p in selected},
                             "observe": "Gather another observation without mutating the factory"},
            }
        }
        for plan in selected:
            pointer = f"`candidate_plans[{json.dumps(plan.id)}]`"
            questions[plan.id + "/benefit"] = {
                "type": "score",
                "instructions": f"How directly does {pointer} advance `active_goal` given `facts`?",
                "criteria": ["No progress", "Indirect", "Useful", "Direct", "Resolves current blocker"],
            }
            questions[plan.id + "/disruption"] = {
                "type": "score",
                "instructions": f"How disruptive is {pointer} to the existing factory in `facts`?",
                "criteria": ["None", "Minor", "Moderate", "Major", "Unacceptable"],
            }
            questions[plan.id + "/needs_observation"] = {
                "type": "noul",
                "instructions": f"Does selecting {pointer} require evidence missing from `facts`?",
            }
        size = len(json.dumps({"state": context, "questions": questions},
                              ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size <= max_bytes:
            return context, questions, selected
        selected = selected[:-1]
    raise ValueError("Decision request exceeds byte budget or has no candidates")


def select_plan(client, state: dict, plans: list[Plan], confidence_floor: float = 0.45,
                max_bytes: int = 32000) -> Decision:
    _number(confidence_floor)
    context, questions, offered = question_batch(state, plans, max_bytes=max_bytes)
    answers = client.evaluate(context, questions)
    try:
        validate_answers(questions, answers)
    except InvalidJudgment as error:
        return Decision(None, "observe", str(error), context, questions,
                        answers if isinstance(answers, dict) else {}, model_called=True)
    choice = answers["candidate"]
    if choice["choice"] == "observe" or choice["confidence"] < confidence_floor:
        return Decision(None, "observe", "Model abstained or choice confidence below floor",
                        context, questions, answers, model_called=True)
    utilities = {}
    for plan in offered:
        benefit = answers[plan.id + "/benefit"]
        disruption = answers[plan.id + "/disruption"]
        if (answers[plan.id + "/needs_observation"]["noul"] >= 0.5
                or min(benefit["confidence"], disruption["confidence"]) < confidence_floor):
            continue
        # Ranking heuristic, NOT a probability of plan success or game victory.
        utilities[plan.id] = (choice["probabilities"][plan.id] + benefit["score"] / 8
                              - disruption["score"] / 16 - len(plan.steps) * 0.02)
    selected = max(utilities, key=utilities.get) if utilities else None
    source = "mock" if getattr(client, "is_mock", False) else "jev"
    return Decision(selected, source if selected else "observe",
                    "" if selected else "Candidate evidence insufficient",
                    context, questions, answers, utilities, model_called=True)
