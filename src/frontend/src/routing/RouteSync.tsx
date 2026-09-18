import { useEffect } from 'react';
import { Outlet, useParams } from 'react-router';
import App from '../App';
import { useChatStore } from '../stores';
import { isAddressableChat } from '../stores/chatStore';

/** App 是常驻外壳，路由子节点只负责「地址 → store」这一个方向的同步，自身不渲染内容，
 *  所以换路由不会重挂载整个聊天界面。
 *
 *  需要同步回来的只剩会话：面板、项目 id 都直接从地址算（usePanel / projectIdFromPath），
 *  没有第二份状态要对齐。会话不同——草稿故意不占地址（`/` 不指向任何一段具体对话），
 *  所以 currentChatId 带着地址给不出的信息，必须真的存一份。 */
export function Shell() {
  return (
    <>
      <App />
      <Outlet />
    </>
  );
}

/** 只在两边不一致时动手：首次进入、前进后退、store 自己推的那次导航跑同一段代码
 *  都收敛到同一结果，以后有人加 <Link> 或深链也不会漏同步。 */
export function ChatRoute() {
  const { chatId } = useParams();
  useEffect(() => {
    const chat = useChatStore.getState();
    if (chatId) {
      if (chat.currentChatId !== chatId) chat.adoptChatFromUrl(chatId);
    } else if (isAddressableChat(chat.currentChatId)) {
      // 退回首页：首页不指向任何一段已有对话，落到一段新草稿上
      chat.newChat();
    }
  }, [chatId]);
  return null;
}
