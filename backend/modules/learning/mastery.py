from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

# BKT 四参数起步值（架构 §11）；改动参数必须同时提升 ALGORITHM_VERSION 并从事件流重建。
BKT_P_INIT = 0.2
BKT_P_TRANSIT = 0.15
BKT_P_GUESS = 0.2
BKT_P_SLIP = 0.1
ALGORITHM_VERSION = "bkt1_fsrs_lite1"
# 少于这个数量的可归因客观事件时对外显示"数据不足"。
MIN_OBJECTIVE_EVENTS = 3
# FSRS 简化版：稳定性初值与答对/答错时的乘子。
_STABILITY_INIT = 1.0
_STABILITY_GAIN = 2.0
_STABILITY_PENALTY = 0.5
_DIFFICULTY_INIT = 5.0

# 只有可判定的客观作答证据驱动数值；追问与用户标记只入事件流。
OBJECTIVE_KINDS = {"attempt_correct", "attempt_incorrect"}
AUXILIARY_KINDS = {"follow_up", "user_override"}
ALL_KINDS = OBJECTIVE_KINDS | AUXILIARY_KINDS


@dataclass(frozen=True)
class MasteryState:
    bkt_p: float
    stability: float
    difficulty: float
    objective_events: int
    last_reviewed_at: str | None
    due_at: str | None

    @classmethod
    def initial(cls) -> "MasteryState":
        return cls(BKT_P_INIT, _STABILITY_INIT, _DIFFICULTY_INIT, 0, None, None)


def bkt_update(prior: float, correct: bool) -> float:
    """经典四参数 BKT 后验：先按观测更新，再叠加学习迁移概率。"""
    if correct:
        likelihood = prior * (1 - BKT_P_SLIP)
        evidence = likelihood + (1 - prior) * BKT_P_GUESS
    else:
        likelihood = prior * BKT_P_SLIP
        evidence = likelihood + (1 - prior) * (1 - BKT_P_GUESS)
    posterior = likelihood / evidence if evidence > 0 else prior
    return min(0.999, posterior + (1 - posterior) * BKT_P_TRANSIT)


def fsrs_rating(kind: str, *, with_hint: bool = False, user_marked_easy: bool = False) -> str:
    """rating 由确定性规则产生，不由 LLM 输出（架构 §11）。"""
    if kind == "attempt_incorrect":
        return "Again"
    if user_marked_easy:
        return "Easy"
    return "Hard" if with_hint else "Good"


def _next_state(state: MasteryState, *, correct: bool, rating: str, now: datetime) -> MasteryState:
    posterior = bkt_update(state.bkt_p, correct)
    if correct:
        multiplier = {"Easy": _STABILITY_GAIN * 1.5, "Good": _STABILITY_GAIN, "Hard": 1.2}.get(rating, _STABILITY_GAIN)
        stability = state.stability * multiplier
        difficulty = max(1.0, state.difficulty - 0.5)
    else:
        stability = max(0.5, state.stability * _STABILITY_PENALTY)
        difficulty = min(10.0, state.difficulty + 1.0)
    # 复习间隔取当前稳定性天数，难度高的概念间隔按比例收紧。
    interval_days = max(0.5, stability * (6.0 / max(1.0, difficulty)))
    return MasteryState(
        bkt_p=posterior, stability=stability, difficulty=difficulty,
        objective_events=state.objective_events + 1,
        last_reviewed_at=now.isoformat(),
        due_at=(now + timedelta(days=interval_days)).isoformat(),
    )


def replay(events: list[dict]) -> MasteryState:
    """从该概念的完整事件流重算状态：投影表可以随时丢弃重建（事件溯源）。"""
    state = MasteryState.initial()
    for event in events:
        kind = event.get("kind")
        if kind not in OBJECTIVE_KINDS:
            continue  # 追问与用户标记默认不进数值更新
        correct = kind == "attempt_correct"
        payload = event.get("payload") or {}
        rating = fsrs_rating(kind, with_hint=bool(payload.get("with_hint")), user_marked_easy=bool(payload.get("marked_easy")))
        created = event.get("created_at")
        try:
            now = datetime.fromisoformat(str(created))
        except (TypeError, ValueError):
            now = datetime.now().astimezone()
        state = _next_state(state, correct=correct, rating=rating, now=now)
    return state


def retention(state: MasteryState, *, at: datetime) -> float:
    """FSRS 半衰期形式的遗忘曲线：距上次复习越久，保持率越低。"""
    if state.last_reviewed_at is None:
        return 1.0
    try:
        last = datetime.fromisoformat(state.last_reviewed_at)
    except ValueError:
        return 1.0
    elapsed_days = max(0.0, (at - last).total_seconds() / 86400)
    return math.exp(-elapsed_days / max(0.5, state.stability))


def mastery_score(state: MasteryState, *, at: datetime) -> float | None:
    """展示与排序用的复合分；证据不足时返回 None，由上层显示"数据不足"。"""
    if state.objective_events < MIN_OBJECTIVE_EVENTS:
        return None
    return round(state.bkt_p * retention(state, at=at), 4)
