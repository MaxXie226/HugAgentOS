import { generatePath, matchPath } from 'react-router';
import type { PanelKey } from '../types';
import { activeProjectId } from '../stores/projectSession';

interface RouterLike {
  navigate: (to: string, opts?: { replace?: boolean }) => unknown;
  subscribe?: (fn: (state: { location: { pathname: string } }) => void) => unknown;
}

/** 路由形状的单一真源：routes.tsx 按它声明路由，这里按它解析 / 生成地址，
 *  转义与末尾斜杠都交给 react-router，不再手写正则。 */
export const CHAT_PATTERN = '/c/:chatId';
export const PROJECT_PATTERN = '/projects/:projectId';

/** 这些 slug 不能和服务端自己占着的路径撞车（nginx 把 `/docs`、`/login`、`/register`、
 *  `/redoc`、`/site/`、`/mock-sso/` 直接转给后端，前端根本收不到），所以文档面板用
 *  `/help` 而不是 `/docs`。 */
const PANEL_SLUGS: Partial<Record<PanelKey, string>> = {
  kb: 'kb',
  docs: 'help',
  app_center: 'app-center',
  settings: 'settings',
  share_records: 'share-records',
  my_space: 'my-space',
  ability_center: 'ability-center',
  lab: 'lab',
  projects: 'projects',
  automation: 'automation',
  sites: 'sites',
};

export const ROUTED_PANELS = Object.entries(PANEL_SLUGS) as Array<[PanelKey, string]>;

const PANEL_BY_SLUG = new Map(ROUTED_PANELS.map(([panel, slug]) => [slug, panel]));

let router: RouterLike | null = null;

/** `router.navigate` 是异步的：同一轮里 `window.location` 还停在旧地址，只靠它判重会让
 *  「选中会话 + 切到聊天面板」这种成对调用把同一次导航发两遍（后一次还会中止前一次）。
 *  这里记住最后一次请求的地址，并订阅 router 在前进 / 后退时把它对回真实位置。 */
let requestedPath: string | null = null;

/** 只有主聊天入口挂 RouterProvider。/admin、/config、/api-docs、分享页是各自独立的
 *  单页应用，不绑定 router——它们复用的 store 里发出的导航请求在那里无处可去。 */
export function bindRouter(r: RouterLike) {
  router = r;
  r.subscribe?.((state) => { requestedPath = state.location.pathname; });
}

/** 聊天面板对应哪个地址取决于当前开着哪段会话，由 chatStore 注册；
 *  避免 navigation → chatStore → navigation 的循环依赖。 */
let chatPath: () => string = () => '/';

export function setChatPathResolver(fn: () => string) {
  chatPath = fn;
}

function currentPath(): string {
  return typeof window === 'undefined' ? '/' : window.location.pathname;
}

/** 是否正停在首页。首页是「一段还没有地址的新对话」，只有它才需要被升级成 `/c/<会话id>`。 */
export function isHomePath(): boolean {
  return currentPath() === '/';
}

export function pathForChat(chatId: string | null): string {
  return chatId ? generatePath(CHAT_PATTERN, { chatId }) : '/';
}

/** `subs` 是面板内部的下级页（能力中心的类别、我的空间的模块、设置的子菜单、
 *  定时任务的任务 id，以及我的空间→知识库→公共/私有这样的第三层）。
 *  给它们一段地址，这些页面才谈得上分享、收藏和刷新回到原处。 */
export function pathForPanel(panel: PanelKey, ...subs: Array<string | null | undefined>): string {
  if (panel === 'chat') return chatPath();
  if (panel === 'project_detail') {
    const projectId = activeProjectId();
    return projectId ? generatePath(PROJECT_PATTERN, { projectId }) : '/projects';
  }
  const slug = PANEL_SLUGS[panel];
  if (!slug) return '/';
  const tail: string[] = [];
  for (const part of subs) {
    if (!part) break;
    tail.push(encodeURIComponent(part));
  }
  return [`/${slug}`, ...tail].join('/');
}

export function chatIdFromPath(pathname: string = currentPath()): string | null {
  return matchPath(CHAT_PATTERN, pathname)?.params.chatId ?? null;
}

export function projectIdFromPath(pathname: string = currentPath()): string | null {
  return matchPath(PROJECT_PATTERN, pathname)?.params.projectId ?? null;
}

function segments(pathname: string): string[] {
  return pathname.replace(/^\/+|\/+$/g, '').split('/').filter(Boolean);
}

export function panelFromPath(pathname: string = currentPath()): PanelKey {
  if (projectIdFromPath(pathname)) return 'project_detail';
  return PANEL_BY_SLUG.get(segments(pathname)[0] ?? '') ?? 'chat';
}

/** 面板地址里一级之后的各段（`/my-space/kb/public` → ['kb','public']）。 */
export function subsFromPath(pathname: string = currentPath()): string[] {
  const parts = segments(pathname);
  if (!parts.length || !PANEL_BY_SLUG.has(parts[0])) return [];
  return parts.slice(1).map(decodeURIComponent);
}

export function navigateTo(to: string, opts?: { replace?: boolean }) {
  if (!router || (requestedPath ?? currentPath()) === to) return;
  requestedPath = to;
  void router.navigate(to, opts);
}
