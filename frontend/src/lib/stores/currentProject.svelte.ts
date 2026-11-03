import type { Project } from '$lib/types';

const STORAGE_KEY = 'mlops_current_project';

function loadFromStorage(): Project | null {
  if (typeof sessionStorage === 'undefined') return null;
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export const projectCtx = $state<{ project: Project | null }>({ project: loadFromStorage() });

export function setCurrentProject(p: Project | null) {
  projectCtx.project = p;
  if (typeof sessionStorage !== 'undefined') {
    if (p) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(p));
    else sessionStorage.removeItem(STORAGE_KEY);
  }
}

export function clearCurrentProject() {
  setCurrentProject(null);
}
