import { isAutomationHistoryChat } from './history';
import type { ChatItem } from '../types';

interface SessionStore {
  chats: Record<string, ChatItem>;
  order: string[];
}

/**
 * 云端会话列表回来重建侧边栏时，挑出「云端名单里没有、但必须留下」的会话。
 *
 * 本机会话本来就不会出现在云端名单里，所以「云端没提到」不是它过期的证据——判定
 * 必须看来源，带本机标记的一律留下。旧判定看的是「本地存过消息没有」，而本机会话
 * 刚并进来时只有标题（正文懒加载），于是每次云端列表回来都把它们当废条目扫掉。
 *
 * 非本机的条目沿用原规则：本地有内容的草稿（服务端还没有）才保留。
 *
 * `live` 是现场 store，`snapshot` 是本轮加载开始时的快照。本机会话可能是快照之后
 * 才并进来的，只看快照会漏掉它们。
 */
export function preservedChatsOnRebuild(
  serverChatIds: ReadonlySet<string>,
  live: SessionStore,
  snapshot: SessionStore,
  isLocalOrigin: (id: string, chat: ChatItem) => boolean,
): { order: string[]; chats: Record<string, ChatItem> } {
  const chats: Record<string, ChatItem> = {};
  const order: string[] = [];
  for (const id of new Set([...live.order, ...snapshot.order])) {
    if (serverChatIds.has(id)) continue;
    const chat = live.chats[id] || snapshot.chats[id];
    if (!chat || isAutomationHistoryChat(chat)) continue;
    const hasMessages = Array.isArray(chat.messages) && chat.messages.length > 0;
    if (!isLocalOrigin(id, chat) && !hasMessages) continue;
    chats[id] = chat;
    order.push(id);
  }
  return { order, chats };
}
