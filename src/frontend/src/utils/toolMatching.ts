/**
 * 把一条 tool_result 事件归到它自己那张工具卡上。
 *
 * 身份只有 tool_call_id。**带了 id 却没找到对应卡片时，绝不能退回按工具名找**——
 * 同名不是身份：并发跑 8 个 read_image 时，同一个名字对应 8 张卡，按名字认领会把
 * 这次输出填进别人的卡里，同时把真正那次调用彻底藏起来。宁可返回 -1，让调用方另
 * 记一条（或补建一张卡），也不要认错人。
 *
 * 只有事件**完全没带 id** 时才允许按名字（再退到「任何还在跑的」）兜底——那种事件
 * 没有别的线索可用。后端 `core/chat/tool_log.py::attach_tool_result` 是同一套规矩。
 */

export function normalizeToolId(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const id = value.trim();
  return id.length > 0 ? id : undefined;
}

export interface ToolCardLike {
  id?: unknown;
  name?: string;
  status?: string;
}

/**
 * 按 id 找卡片；同一个 id 有多张时认还没收口的那张。
 *
 * 按响应重新编号的网关会让 `call_0` 既指上一轮已经跑完的调用，也指这一轮正在跑的
 * 调用。认第一张的话，第二次调用会把第一次的参数和输出整个盖掉——一整次调用就这么
 * 没了。后端 `core/chat/tool_log.py::upsert_tool_call` 是同一套规矩。
 */
export function toolCardIndexById(cards: ToolCardLike[], toolId: string): number {
  let settled = -1;
  for (let i = 0; i < cards.length; i++) {
    if (normalizeToolId(cards[i].id) !== toolId) continue;
    if (cards[i].status === 'running' || cards[i].status === 'pending') return i;
    if (settled < 0) settled = i;
  }
  return settled;
}

export function lastRunningToolIndex(cards: ToolCardLike[], name?: string): number {
  for (let i = cards.length - 1; i >= 0; i--) {
    if (cards[i].status !== 'running') continue;
    if (name && cards[i].name !== name) continue;
    return i;
  }
  return -1;
}

export function resolveToolCardIndex(
  cards: ToolCardLike[],
  eventToolId: string | undefined,
  eventToolName: string | undefined,
  options: { fallbackToAnyRunning?: boolean } = {},
): number {
  if (eventToolId) {
    // 带 id 就以 id 为准；找不到就是找不到，不再往下猜。
    return toolCardIndexById(cards, eventToolId);
  }
  if (eventToolName) {
    const byName = lastRunningToolIndex(cards, eventToolName);
    if (byName >= 0) return byName;
  }
  return options.fallbackToAnyRunning ? lastRunningToolIndex(cards) : -1;
}

/**
 * 把一条 subagent_event 归到它所属的父工具卡（`call_subagent` / `run_job`）上。
 *
 * 和上面同一条规矩：事件报了 parent_tool_id 就只认这个 id。并行调多个子智能体时，
 * 「最后一张 call_subagent 卡」同时对应好几个还在跑的子智能体，按名字认领会把 A 的
 * 思考和子工具塞进 B 的卡里。认不到就返回 -1，调用方直接丢弃这条事件。
 *
 * 名字兜底只留给**完全没带 parent_tool_id** 的事件：批量作业的 job_progress 在拿不到
 * CURRENT_TOOL_CALL_ID 时就是这种形状，它除了 `run_job` 这个名字没有别的线索。
 */
export function resolveSubagentParentIndex(
  cards: ToolCardLike[],
  parentToolId: string | undefined,
  parentToolName: string,
): number {
  if (parentToolId) {
    // 子步骤是父调用还在跑的时候冒出来的 → 同 id 认未收口的那张（同 toolCardIndexById）。
    return toolCardIndexById(cards, parentToolId);
  }
  for (let i = cards.length - 1; i >= 0; i--) {
    if (cards[i]?.name === parentToolName) return i;
  }
  return -1;
}
