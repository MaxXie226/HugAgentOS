import { createLocalProject } from '../api';
import type { ProjectDetail } from '../types';

/** The menu owns a separate event so page-level folder pickers cannot also create a project. */
export function listenForFolderProjects(
  target: EventTarget,
  open: (project: ProjectDetail) => void,
  reportError: (error: unknown) => void,
  navigationKey: () => string = () => '',
) {
  let pending = false;
  let active = true;
  const onFolder = async (event: Event) => {
    event.preventDefault();
    const startedAt = navigationKey();
    const path = (event as CustomEvent<unknown>).detail;
    if (pending || typeof path !== 'string' || !path) return;
    pending = true;
    try {
      const name = path.split(/[/\\]/).filter(Boolean).pop() || path;
      const project = await createLocalProject({ name, local_path: path });
      if (active && startedAt === navigationKey()) open(project);
    } catch (error) {
      if (active && startedAt === navigationKey()) reportError(error);
    } finally {
      pending = false;
    }
  };
  target.addEventListener('hugagent:open-project-folder', onFolder);
  return () => {
    active = false;
    target.removeEventListener('hugagent:open-project-folder', onFolder);
  };
}
