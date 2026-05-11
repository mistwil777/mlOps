import type { Project } from '$lib/types';

export const projectCtx = $state<{ project: Project | null }>({ project: null });

export function setCurrentProject(p: Project | null) {
  projectCtx.project = p;
}

export function clearCurrentProject() {
  projectCtx.project = null;
}
