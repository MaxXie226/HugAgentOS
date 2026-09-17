import assert from 'node:assert/strict';

import { preservedChatsOnRebuild } from '../src/utils/sessionRebuild';
import type { ChatItem } from '../src/types';

const chat = (id: string, extra: Partial<ChatItem> = {}): ChatItem => ({
  id,
  title: id,
  createdAt: 1,
  updatedAt: 1,
  messages: [],
  ...extra,
} as ChatItem);

const store = (...items: ChatItem[]) => ({
  chats: Object.fromEntries(items.map((c) => [c.id, c])),
  order: items.map((c) => c.id),
});

const isLocalOrigin = (_id: string, c: ChatItem) => c.runTarget === 'local';

{
  // 回归主场景：本机会话在启动快照之后才并进来，只有标题没有正文。云端列表回来
  // 重建侧边栏时必须留下它——旧判定看「本地存过消息没有」，会把它整条扫掉。
  const local = chat('local-1', { runTarget: 'local' });
  const kept = preservedChatsOnRebuild(new Set(['cloud-1']), store(local), store(), isLocalOrigin);
  assert.deepEqual(kept.order, ['local-1']);
  assert.equal(kept.chats['local-1'], local);
}

{
  // 刷新后本机会话先从 localStorage 快照恢复、云端列表抢先回来：同样必须留下。
  const local = chat('local-1', { runTarget: 'local' });
  const kept = preservedChatsOnRebuild(new Set(), store(), store(local), isLocalOrigin);
  assert.deepEqual(kept.order, ['local-1']);
}

{
  // 非本机的条目沿用原规则：本地有内容的草稿留下，空壳条目丢掉。
  const draft = chat('draft', { messages: [{ role: 'user', content: 'hi' }] as ChatItem['messages'] });
  const empty = chat('empty');
  const kept = preservedChatsOnRebuild(new Set(), store(draft, empty), store(), isLocalOrigin);
  assert.deepEqual(kept.order, ['draft']);
}

{
  // 云端名单里已有的不重复保留；自动化历史条目一律不保留（哪怕标着本机）。
  const kept = preservedChatsOnRebuild(
    new Set(['cloud-1']),
    store(
      chat('cloud-1', { messages: [{ role: 'user', content: 'hi' }] as ChatItem['messages'] }),
      chat('auto-1', { runTarget: 'local', automationRun: true }),
    ),
    store(),
    isLocalOrigin,
  );
  assert.deepEqual(kept.order, []);
}

{
  // 现场 store 与快照都有同一条时只保留一次，并以现场那份为准（标题可能刚改过）。
  const live = chat('local-1', { runTarget: 'local', title: 'renamed' });
  const stale = chat('local-1', { runTarget: 'local', title: 'old' });
  const kept = preservedChatsOnRebuild(new Set(), store(live), store(stale), isLocalOrigin);
  assert.deepEqual(kept.order, ['local-1']);
  assert.equal(kept.chats['local-1'].title, 'renamed');
}

console.log('local session preserve: all assertions passed');
