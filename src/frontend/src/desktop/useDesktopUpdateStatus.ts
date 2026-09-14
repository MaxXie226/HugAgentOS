import { useDeploymentModeStore } from '../stores/deploymentModeStore';

export type { DesktopUpdateStatus } from '../stores/deploymentModeStore';

/** Reuse the desktop event stream; unchanged snapshots retain object identity. */
export function useDesktopUpdateStatus() {
  return useDeploymentModeStore((s) => s.desktopUpdate);
}
