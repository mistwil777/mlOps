<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getModels } from '$lib/api';
  import type { DeployedModel } from '$lib/types';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import SectionTabs from '$lib/components/SectionTabs.svelte';
  const tabs = [{ href: '/delivery', label: 'Bundle client' }, { href: '/lifecycle', label: 'Cycle de vie' }];

  let models: DeployedModel[] = $state([]);
  let complianceReport: Record<string, any> | null = $state(null);
  let complianceModelId: string | null = $state(null);
  let complianceLoading = $state(false);

  onMount(async () => {
    models = await getModels().catch(() => []);
  });

  async function loadCompliance(modelId: string) {
    if (complianceModelId === modelId) { complianceReport = null; complianceModelId = null; return; }
    complianceLoading = true;
    complianceModelId = modelId;
    try {
      const r = await fetch(`/api/models/${modelId}/compliance`);
      if (!r.ok) throw new Error(await r.text());
      complianceReport = await r.json();
    } catch {
      complianceReport = null;
    } finally {
      complianceLoading = false;
    }
  }

  function statusIcon(s: string) {
    return s === 'pass' ? '✓' : s === 'fail' ? '✗' : s === 'warning' ? '⚠' : '—';
  }
  function statusClass(s: string) {
    return s === 'pass' ? 'comp-pass' : s === 'fail' ? 'comp-fail' : s === 'warning' ? 'comp-warn' : 'comp-na';
  }
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
            <td style="display:flex;gap:6px">
              <button class="btn btn-ghost" onclick={() => loadCompliance(m.id)}>
                {complianceLoading && complianceModelId === m.id ? '…' : '⚖ Audit'}
              </button>
              <button class="btn btn-ghost" onclick={() => goto(`/delivery/${m.id}`)}>↓ Livrer</button>
            </td>
          </tr>
        {:else}
          <tr><td colspan="7" style="text-align:center;padding:24px;color:var(--text-muted)">Aucun modèle disponible à la livraison</td></tr>
        {/each}
      </tbody>
    </table>
  </div>

  <!-- Compliance report panel -->
  {#if complianceReport}
    <div class="card comp-card">
      <div class="comp-header">
        <div>
          <span class="comp-title">Audit conformité — {complianceReport.model_id}</span>
          <span class="text-muted" style="font-size:11px; margin-left:8px">{complianceReport.generated_at}</span>
        </div>
        <div class="comp-badges">
          <span class="comp-badge comp-pass">{complianceReport.passed} ✓</span>
          {#if complianceReport.warnings > 0}
            <span class="comp-badge comp-warn">{complianceReport.warnings} ⚠</span>
          {/if}
          {#if complianceReport.failed > 0}
            <span class="comp-badge comp-fail">{complianceReport.failed} ✗</span>
          {/if}
          <span class="comp-badge {complianceReport.delivery_blocked ? 'comp-fail' : 'comp-pass'}" style="font-weight:700">
            {complianceReport.delivery_blocked ? 'LIVRAISON BLOQUÉE' : 'CONFORME'}
          </span>
        </div>
      </div>

      <div class="comp-checks">
        {#each complianceReport.checks as c}
          <div class="comp-check {statusClass(c.status)}">
            <div class="comp-check-top">
              <span class="comp-icon">{statusIcon(c.status)}</span>
              <span class="comp-fw">{c.framework}</span>
              <span class="comp-check-title">{c.title}</span>
            </div>
            <div class="comp-evidence">{c.evidence}</div>
            {#if c.recommendation}
              <div class="comp-reco">→ {c.recommendation}</div>
            {/if}
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .comp-card { margin-top: 16px; padding: 20px; }
  .comp-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:8px; }
  .comp-title { font-size:14px; font-weight:700; }
  .comp-badges { display:flex; gap:6px; flex-wrap:wrap; }
  .comp-badge { padding:3px 10px; border-radius:999px; font-size:11px; font-weight:600; }
  .comp-pass { background:#dcfce7; color:#166534; }
  .comp-fail { background:#fee2e2; color:#991b1b; }
  .comp-warn { background:#fef9c3; color:#854d0e; }
  .comp-na   { background:#f1f5f9; color:#64748b; }
  .comp-checks { display:flex; flex-direction:column; gap:8px; }
  .comp-check { padding:10px 12px; border-radius:6px; border-left:3px solid transparent; }
  .comp-check.comp-pass { background:#f0fdf4; border-color:#22c55e; }
  .comp-check.comp-fail { background:#fef2f2; border-color:#ef4444; }
  .comp-check.comp-warn { background:#fefce8; border-color:#eab308; }
  .comp-check.comp-na   { background:#f8fafc; border-color:#cbd5e1; }
  .comp-check-top { display:flex; align-items:center; gap:8px; margin-bottom:4px; }
  .comp-icon { font-size:12px; font-weight:700; width:14px; }
  .comp-fw { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; color:var(--text-muted); }
  .comp-check-title { font-size:13px; font-weight:600; }
  .comp-evidence { font-size:11px; color:var(--text-muted); margin-left:22px; }
  .comp-reco { font-size:11px; color:#92400e; margin-left:22px; margin-top:3px; font-style:italic; }
</style>
