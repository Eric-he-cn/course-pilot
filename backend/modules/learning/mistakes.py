from __future__ import annotations

from dataclasses import dataclass

from .mastery import OBJECTIVE_KINDS

# 连续答对这么多次就毕业。跨 session 累计，答错一次归零。
GRADUATE_STREAK = 2


@dataclass(frozen=True)
class MistakeState:
    """某个概念的错题状态；wrong_count 是累计错次，毕业不清零。"""

    status: str
    wrong_count: int
    streak: int
    first_wrong_at: str
    last_wrong_at: str
    graduated_at: str | None
    relapse_count: int


def _timestamp(event: dict) -> str:
    """直接留事件的原始时间戳，不做解析——重放要能逐位复现。"""
    return str(event.get("created_at") or "")


def _on_wrong(state: MistakeState | None, at: str) -> MistakeState:
    if state is None:
        return MistakeState("active", 1, 0, at, at, None, 0)
    relapsed = state.status == "graduated"
    return MistakeState(
        status="active", wrong_count=state.wrong_count + 1, streak=0,
        first_wrong_at=state.first_wrong_at, last_wrong_at=at, graduated_at=None,
        relapse_count=state.relapse_count + (1 if relapsed else 0),
    )


def _on_correct(state: MistakeState, at: str) -> MistakeState:
    streak = state.streak + 1
    graduating = state.status == "active" and streak >= GRADUATE_STREAK
    return MistakeState(
        status="graduated" if graduating else state.status,
        wrong_count=state.wrong_count, streak=streak,
        first_wrong_at=state.first_wrong_at, last_wrong_at=state.last_wrong_at,
        graduated_at=at if graduating else state.graduated_at,
        relapse_count=state.relapse_count,
    )


def replay_mistakes(events: list[dict]) -> MistakeState | None:
    """从该概念的完整事件流重算错题状态；从没错过则返回 None（不建行）。

    进入投影的闸门与掌握度一致：只有可判定的客观作答证据参与，追问与用户标记不算。
    """
    state: MistakeState | None = None
    for event in events:
        kind = event.get("kind")
        if kind not in OBJECTIVE_KINDS:
            continue
        if kind == "attempt_incorrect":
            state = _on_wrong(state, _timestamp(event))
        elif state is not None:
            state = _on_correct(state, _timestamp(event))
    return state
