import asyncio
import pytest

from amc.context import Lane, Role, lane_scope, current_lane
from amc.policy import classify, Isolation
from amc.interceptor import wrap, EVENTS

REAL_CALLS: list[str] = []


async def read_docs(q):
    REAL_CALLS.append("read_docs")
    return f"docs for {q}"


async def send_email(to):
    REAL_CALLS.append("send_email")
    return {"sent": to}


async def mystery_tool():
    REAL_CALLS.append("mystery_tool")
    return "?"


POLICIES = classify(
    ["read_docs", "send_email"],
    annotations={
        "read_docs":  {"readOnlyHint": True,  "destructiveHint": False},
        "send_email": {"readOnlyHint": False, "destructiveHint": True},
    },
)

docs = wrap(read_docs, "read_docs", POLICIES)
email = wrap(send_email, "send_email", POLICIES)
mystery = wrap(mystery_tool, "mystery_tool", POLICIES)


async def agent():
    await docs("q")
    await email("user@example.com")
    await mystery()


@pytest.mark.asyncio
async def test_only_primary_performs_side_effects():
    REAL_CALLS.clear(); EVENTS.clear()

    async def run(lane):
        with lane_scope(lane):
            await agent()

    await asyncio.gather(
        run(Lane("l0", Role.PRIMARY)),
        run(Lane("l1", Role.SHADOW, "claude")),
        run(Lane("l2", Role.SHADOW, "gemini")),
    )

    assert REAL_CALLS.count("send_email") == 1
    assert REAL_CALLS.count("mystery_tool") == 1      # primary only
    assert REAL_CALLS.count("read_docs") == 3         # safe: all lanes

    blocked = [e for e in EVENTS if not e["executed"]]
    assert len(blocked) == 4                          # 2 shadows x (email + mystery)
    assert any(e["reason"] == "blocked:annotation" for e in blocked)
    assert any(e["reason"] == "unclassified" for e in blocked)


@pytest.mark.asyncio
async def test_unset_lane_blocks():
    REAL_CALLS.clear()
    await email("x@y.com")                            # no lane_scope at all
    assert REAL_CALLS == []


@pytest.mark.asyncio
async def test_context_isolation():
    seen = []

    async def peek(lane):
        with lane_scope(lane):
            await asyncio.sleep(0.01)
            seen.append(current_lane().id)

    await asyncio.gather(*[peek(Lane(f"l{i}", Role.SHADOW)) for i in range(5)])
    assert sorted(seen) == [f"l{i}" for i in range(5)]
    assert current_lane() is None