import { create } from 'zustand';
import type { Catalog, KbTabKey, PanelKey } from '../types';
import { getCatalog, updateCatalogItem } from '../api';
import { loadCatalog, saveCatalog } from '../storage';
import { navigateTo, pathForPanel } from '../routing/navigation';
import { pathForKbTab } from '../routing/subPages';

/**
 * 「当前停在哪个面板 / 哪个二级页」一律由地址栏决定（`/my-space/kb/public`、
 * `/ability-center/connectors` …），store 与浏览器存储里都不再留第二份。
 * 组件用 usePanel / useAbilityTab / useMySpaceTab / useKbTab 从地址实时算。
 */

interface CatalogState {
  catalog: Catalog;
  catalogLoading: boolean;
  /** 每进入一次顶层面板 +1。它不是「当前在哪个面板」的副本，而是「又进了一次」这个动作，
   *  各页据此重放入场动画 / 重新拉列表。 */
  panelEntryNonce: number;
  /** Search query within catalog management */
  manageQuery: string;
  /** Selected catalog item id */
  selectedId: string | null;

  setCatalog: (catalog: Catalog) => void;
  setCatalogLoading: (v: boolean) => void;
  /** 切换面板 = 跳到该面板的地址；`sub` 是要直接落到的二级页（如能力中心的类别）。
   *  二级页必须随这一次跳转一起给出——先跳类别再跳面板会把类别冲掉。 */
  setPanel: (panel: PanelKey, sub?: string) => void;
  setManageQuery: (query: string) => void;
  setSelectedId: (id: string | null) => void;
  setKbTab: (tab: KbTabKey) => void;

  /** Fetch catalog from backend, merge with localStorage enabled state */
  fetchCatalog: () => Promise<void>;
  /** Toggle item enabled/disabled (optimistic update + backend sync) */
  toggleItem: (kind: 'skills' | 'agents' | 'mcp' | 'kb', itemId: string, enabled: boolean) => Promise<void>;
}

export const useCatalogStore = create<CatalogState>((set, get) => ({
  catalog: loadCatalog(),
  catalogLoading: true,
  panelEntryNonce: 0,
  manageQuery: '',
  selectedId: null,

  setCatalog: (catalog) => {
    set({ catalog });
    saveCatalog(catalog);
  },
  setCatalogLoading: (v) => set({ catalogLoading: v }),
  setPanel: (panel, sub) => {
    set((state) => ({
      panelEntryNonce: state.panelEntryNonce + 1,
      selectedId: null,
      manageQuery: '',
    }));
    navigateTo(pathForPanel(panel, sub));
  },
  setManageQuery: (query) => set({ manageQuery: query }),
  setSelectedId: (id) => set({ selectedId: id }),
  setKbTab: (tab) => {
    set({ manageQuery: '' });
    navigateTo(pathForKbTab(tab));
  },

  fetchCatalog: async () => {
    try {
      set({ catalogLoading: true });
      const remote = await getCatalog();
      // Merge local enabled states onto remote catalog
      const local = loadCatalog();
      const mergeEnabled = <T extends { id: string; enabled: boolean }>(
        remoteItems: T[],
        localItems: { id: string; enabled: boolean }[],
      ): T[] => {
        const localMap = new Map(localItems.map((i) => [i.id, i.enabled]));
        return remoteItems.map((item) => ({
          ...item,
          enabled: localMap.has(item.id) ? localMap.get(item.id)! : item.enabled,
        }));
      };
      const merged: Catalog = {
        skills: mergeEnabled(remote.skills, local.skills),
        agents: mergeEnabled(remote.agents, local.agents),
        mcp: mergeEnabled(remote.mcp, local.mcp),
        kb: mergeEnabled(remote.kb, local.kb),
      };
      set({ catalog: merged, catalogLoading: false });
      saveCatalog(merged);
    } catch (e) {
      console.error('Failed to fetch catalog:', e);
      set({ catalogLoading: false });
    }
  },

  toggleItem: async (kind, itemId, enabled) => {
    const { catalog } = get();
    // Optimistic update
    const updated = {
      ...catalog,
      [kind]: catalog[kind].map((item) =>
        item.id === itemId ? { ...item, enabled } : item,
      ),
    };
    set({ catalog: updated });
    saveCatalog(updated);
    // Sync to backend
    try {
      await updateCatalogItem(kind, itemId, enabled);
    } catch (e) {
      console.error('Failed to sync catalog toggle:', e);
    }
  },
}));
