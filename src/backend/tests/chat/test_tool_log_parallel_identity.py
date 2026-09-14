"""并行调用下，工具日志里的每一条都必须还是它自己。

同名不是身份，缺席的 id 也不是身份。这里锁两条：子智能体的并行子工具不会因为都没有
id 就被并成一条；结果找不到自己的调用时，补录的那条会自报家门（``call_missing``），
而不是伪装成一次正常的零参调用。
"""

from core.chat.tool_log import attach_subagent_step, attach_tool_result, upsert_tool_call


def test_two_id_less_sub_tools_stay_two_steps():
    log: list = [{"tool_name": "call_subagent", "tool_id": "parent"}]
    attach_subagent_step(
        log, "parent", {"sub_type": "tool_call", "tool_name": "bash", "input": {"cmd": "a"}}
    )
    attach_subagent_step(
        log, "parent", {"sub_type": "tool_call", "tool_name": "bash", "input": {"cmd": "b"}}
    )

    steps = log[0]["sub_steps"]
    assert [step["input"] for step in steps] == [{"cmd": "a"}, {"cmd": "b"}]


def test_sub_steps_that_do_have_ids_still_merge():
    log: list = [{"tool_name": "call_subagent", "tool_id": "parent"}]
    attach_subagent_step(
        log,
        "parent",
        {"sub_type": "tool_call", "tool_id": "s1", "tool_name": "bash", "input": {"cmd": "a"}},
    )
    attach_subagent_step(
        log,
        "parent",
        {"sub_type": "tool_result", "tool_id": "s1", "tool_name": "bash", "output": "done"},
    )

    (step,) = log[0]["sub_steps"]
    assert step["input"] == {"cmd": "a"} and step["output"] == "done"


def test_a_result_without_its_call_says_so():
    log: list = []
    upsert_tool_call(
        log, {"tool_name": "read_image", "tool_id": "call_a", "tool_args": {"path": "a.png"}}
    )
    attach_tool_result(log, "call_z", "read_image", {"source": "z.png"})

    original, recorded = log
    assert "result" not in original, "别人的结果不该落在这张卡上"
    assert recorded["call_missing"] is True
    assert "duration_ms" not in recorded, "没有开始时刻就不编造耗时"


def test_a_normal_result_is_not_flagged():
    log: list = []
    upsert_tool_call(log, {"tool_name": "bash", "tool_id": "call_a", "tool_args": {}})
    attach_tool_result(log, "call_a", "bash", {"stdout": "ok"})

    (entry,) = log
    assert "call_missing" not in entry


def test_an_id_reused_after_its_call_finished_opens_a_new_entry():
    """一条消息横跨多轮 ReAct；按响应重新编号的网关会在下一轮再发一次 call_0。"""
    log: list = []
    upsert_tool_call(log, {"tool_name": "bash", "tool_id": "call_0", "tool_args": {"cmd": "a"}})
    attach_tool_result(log, "call_0", "bash", {"stdout": "a"})
    upsert_tool_call(log, {"tool_name": "bash", "tool_id": "call_0", "tool_args": {"cmd": "b"}})
    attach_tool_result(log, "call_0", "bash", {"stdout": "b"})

    first, second = log
    assert (first["tool_args"], first["result"]) == ({"cmd": "a"}, {"stdout": "a"})
    assert (second["tool_args"], second["result"]) == ({"cmd": "b"}, {"stdout": "b"})


def test_arguments_still_backfill_into_the_open_call():
    """流式首帧常常没有参数，后一帧补齐——这仍然是同一次调用，不能变成两条。"""
    log: list = []
    upsert_tool_call(log, {"tool_name": "bash", "tool_id": "call_0", "tool_args": {}})
    upsert_tool_call(log, {"tool_name": "bash", "tool_id": "call_0", "tool_args": {"cmd": "a"}})

    (entry,) = log
    assert entry["tool_args"] == {"cmd": "a"}
