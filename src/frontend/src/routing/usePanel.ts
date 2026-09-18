import { useLocation } from 'react-router';
import type { PanelKey } from '../types';
import { panelFromPath, projectIdFromPath, subsFromPath } from './navigation';

/** 「当前停在哪个面板」直接从地址算出来，不另存一份。地址是唯一真源，
 *  刷新、前进后退、粘贴链接进来都只有一个答案。 */
export function usePanel(): PanelKey {
  return panelFromPath(useLocation().pathname);
}

/** 「当前打开的是哪个项目」同样只认地址（`/projects/<项目id>`）。 */
export function useRouteProjectId(): string | null {
  return projectIdFromPath(useLocation().pathname);
}

/** 面板内部的下级页（能力中心的类别、我的空间的模块、设置的子菜单…）同样只认地址。 */
export function useRouteSubs(): string[] {
  return subsFromPath(useLocation().pathname);
}
