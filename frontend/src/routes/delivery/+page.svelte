<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getModels } from '$lib/api';
  import type { DeployedModel } from '$lib/types';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import SectionTabs from '$lib/components/SectionTabs.svelte';
  const tabs = [{ href: '/delivery', label: 'Bundle client' }, { href: '/lifecycle', label: 'Cycle de vie' }];

  let models: DeployedModel[] = $state([]);

  onMount(async () => {
    models = await getModels().catch(() => []);
  });
</script>

<SectionTabs {tabs} />
<div class="topbar">
  <span class="topbar-title">Livraison</span>
</div>

<div class="content">
  <div class="card">
    <table class="table">
      <thead>
        <tr><th>Modèle</th><th>Projet</th><th>Val loss</th><th>Taille</th><th>Statut</th><th>Déployé</th><th></th></tr>
      </thead>
      <tbody>
        {#each models as m}
          <tr>
            <td class="mono text-accent">{m.id}</td>
            <td>{m.project_name ?? m.project_id}</td>
            <td class="mono">{m.val_loss?.toFixed(4) ?? '—'}</td>
            <td>{m.size_mb ? `${m.size_mb} MB` : '—'}</td>
            <td><StatusBadge status={m.status} /></td>
            <td class="text-muted">{m.deployed_at?.slice(0,10)}</td>
            <td><button class="btn btn-ghost" onclick={() => goto(`/delivery/${m.id}`)}>↓ Livrer</button></td>
          </tr>
        {:else}
          <tr><td colspan="7" style="text-align:center;padding:24px;color:var(--text-muted)">Aucun modèle disponible à la livraison</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</div>
