<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getProjects } from '$lib/api';
  import type { Project } from '$lib/types';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import TaskBadge from '$lib/components/TaskBadge.svelte';

  let projects: Project[] = $state([]);
  let loading = $state(true);

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    loading = false;
  });
</script>

<div class="topbar">
  <span class="topbar-title">Projets</span>
  <a href="/projects/new" class="btn btn-primary">+ Nouveau projet</a>
</div>

<div class="content">
  <div class="card">
    {#if loading}
      <p class="text-muted" style="padding:24px;text-align:center">Chargement…</p>
    {:else}
      <table class="table">
        <thead>
          <tr>
            <th>Nom</th><th>Domaine / Tâche</th><th>Modèle base</th>
            <th>Runs</th><th>Dernier statut</th><th>Mis à jour</th>
          </tr>
        </thead>
        <tbody>
          {#each projects as p}
            <tr onclick={() => goto(`/projects/${p.id}`)}>
              <td>
                <div style="font-weight:600">{p.name}</div>
                <div class="text-muted" style="font-size:12px">{p.classification}</div>
              </td>
              <td><TaskBadge domain={p.domain} taskType={p.task_type} /></td>
              <td class="mono" style="font-size:12px">{p.model}</td>
              <td>{p.n_runs}</td>
              <td>{#if p.last_status}<StatusBadge status={p.last_status} />{:else}—{/if}</td>
              <td class="text-muted">{p.db?.updated_at?.toString().slice(0, 10) ?? '—'}</td>
            </tr>
          {:else}
            <tr>
              <td colspan="6" style="text-align:center;color:var(--text-muted);padding:32px">
                Aucun projet. <a href="/projects/new">Créer le premier</a>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</div>
