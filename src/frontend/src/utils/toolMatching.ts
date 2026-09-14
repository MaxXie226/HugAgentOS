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
    return cards.findIndex((card) => normalizeToolId(card.id) === eventToolId);
  }
  if (eventToolName) {
    const byName = lastRunningToolIndex(cards, eventToolName);
    if (byName >= 0) return byName;
  }
  return options.fallbackToAnyRunning ? lastRunningToolIndex(cards) : -1;
}
