"""Cursor SDK helpers shared by qc_agent and source_agent.

同步 API 在 Windows 上有 select() 管道 bug，调用方必须走 AsyncClient。
内容安全拒图是确定性的，换模型才有用，重试同一模型没用。
"""

from __future__ import annotations

import asyncio
import re

RETRIES = 3


def model_selection(model: str, params: list[str]):
    """`--param effort=low` -> ModelSelection。不给 param 就用该模型的默认变体。"""
    from cursor_sdk import ModelParameterValue, ModelSelection

    if not params:
        return model
    vals = []
    for p in params:
        k, _, v = p.partition("=")
        if not v:
            raise SystemExit(f"--param 要写成 id=value，收到 {p!r}")
        vals.append(ModelParameterValue(id=k.strip(), value=v.strip()))
    return ModelSelection(id=model, params=tuple(vals))


def run_tag(model: str, params: list[str]) -> str:
    bits = [model] + [p.replace("=", "-") for p in sorted(params)]
    return re.sub(r"[^\w.-]+", "_", "-".join(bits))


def tokens_of(result) -> tuple[int, int] | None:
    """本地 run 只报 TokenUsage，不报钱。返回 (输入, 输出)。"""
    u = getattr(result, "usage", None)
    if u is None:
        return None
    return getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0


def plan_opts(model, cwd: str):
    from cursor_sdk import AgentOptions, LocalAgentOptions

    return AgentOptions(
        model=model,
        mode="plan",
        local=LocalAgentOptions(cwd=cwd),
    )


async def prompt_with_retry(client, msg, model, fallback, cwd: str, retries: int = RETRIES):
    """成功返回 (result, used_model, None)；全失败返回 (None, None, error)。"""
    from cursor_sdk import AsyncAgent

    plan = [(model, i) for i in range(retries)]
    if fallback:
        plan.append((fallback, 0))

    result = last = used = None
    for m, i in plan:
        if i:
            await asyncio.sleep(2 * i)
        try:
            result, used = await AsyncAgent.prompt(msg, plan_opts(m, cwd), client=client), m
            if result.status == "finished":
                return result, used, None
            last = f"run 状态 {result.status}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
    extra = f" + 兜底 {fallback}" if fallback else ""
    return None, None, f"{last}（{retries} 次{extra}均失败）"
