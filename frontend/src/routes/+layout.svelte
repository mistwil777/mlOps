<script lang="ts">
  import '../app.css';
  import { getHealth } from '$lib/api';
  import SidebarNav from '$lib/components/SidebarNav.svelte';
  import type { Health } from '$lib/types';

  let { children } = $props();
  let health: Health | null = $state(null);

  async function refreshHealth() {
    health = await getHealth().catch(() => null);
  }
  refreshHealth();
  setInterval(refreshHealth, 15000);
</script>

<div class="layout">
  <SidebarNav>
    {#snippet healthPanel()}
      <div class="health-panel">
        <div class="health-label">Services</div>
        <div class="health-row">
          <span class="health-dot {health?.mlx_server ? 'ok' : health === null ? 'unk' : 'err'}"></span>
          <span>mlx_lm.server</span>
        </div>
        <div class="health-row">
          <span class="health-dot {health?.mlflow ? 'ok' : health === null ? 'unk' : 'err'}"></span>
          <span>MLflow</span>
        </div>
        <div class="health-row">
          <span class="health-dot {health?.serving ? 'ok' : health === null ? 'unk' : 'err'}"></span>
          <span>Serving</span>
        </div>
      </div>
    {/snippet}
  </SidebarNav>

  <main class="main">
    {@render children()}
  </main>
</div>

<style>
  .health-panel {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .health-label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--text-dim);
    font-weight: 600;
    margin-bottom: 2px;
  }

  .health-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
</style>
