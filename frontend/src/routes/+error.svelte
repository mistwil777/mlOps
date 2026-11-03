<script lang="ts">
  import { page } from '$app/state';
</script>

<div style="padding: 40px; font-family: monospace; max-width: 900px;">
  <h1 style="color: #e66767; margin-bottom: 8px;">Erreur {page.status}</h1>
  <p style="color:#555; font-size:14px; margin-bottom:16px;">Chemin : {page.url?.pathname ?? '—'}</p>
  <pre style="background:#fff0f0; border:1px solid #e66767; padding:16px; border-radius:8px; overflow:auto; font-size:13px; color:#333; white-space:pre-wrap;">{page.error?.message ?? 'Erreur inconnue (message vide — hooks.client.ts non exécuté)'}</pre>
  {#if page.error?.stack}
  <pre style="background:#f5f5f5; padding:16px; border-radius:8px; overflow:auto; font-size:11px; color:#666; margin-top:12px; white-space:pre-wrap;">{page.error.stack}</pre>
  {/if}
</div>
