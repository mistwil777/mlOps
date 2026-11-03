import type { HandleClientError } from '@sveltejs/kit';

export const handleError: HandleClientError = ({ error }) => {
  const msg = error instanceof Error ? error.message : String(error);
  const stack = error instanceof Error ? (error.stack ?? '') : '';
  // Inject into DOM so it survives even if +error.svelte has issues
  try {
    const div = document.createElement('div');
    div.id = '__sveltekit_error_debug__';
    div.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;background:#e66767;color:#fff;padding:12px;font:13px monospace;white-space:pre-wrap;';
    div.textContent = `CLIENT ERROR: ${msg}\n${stack}`;
    document.body.appendChild(div);
  } catch { /* noop */ }
  return { message: msg, stack };
};
