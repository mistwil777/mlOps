<script lang="ts">
  import { onMount } from 'svelte';
  import Topbar from '$lib/components/Topbar.svelte';
  import { getCorrections, getMemoryStats, validateCorrection, rejectCorrection } from '$lib/api';
  import type { Correction, MemoryStats } from '$lib/api';

  type Tab = 'staging' | 'validated' | 'rejected';

  let activeTab = $state<Tab>('staging');
  let stats = $state<MemoryStats | null>(null);
  let corrections = $state<Correction[]>([]);
  let loading = $state(false);
  let rejectTarget = $state<string | null>(null);
  let rejectReason = $state('');
  let actionLoading = $state<string | null>(null);

  const stagingCount = $derived(stats?.by_phase
    ? Object.values(stats.by_phase).reduce((s, p) => s + (p.staging ?? 0), 0)
    : 0
  );

  async function loadTab(tab: Tab) {
    activeTab = tab;
    loading = true;
    corrections = [];
    try {
      corrections = await getCorrections(tab);
    } catch { corrections = []; }
    finally { loading = false; }
  }

  async function loadStats() {
    try { stats = await getMemoryStats(); } catch { /* silent */ }
  }

  async function doValidate(id: string) {
    actionLoading = id;
    try {
      await validateCorrection(id);
      corrections = corrections.filter(c => c.id !== id);
      await loadStats();
    } finally { actionLoading = null; }
  }

  async function doReject(id: string) {
    if (!rejectReason.trim()) return;
    actionLoading = id;
    try {
      await rejectCorrection(id, rejectReason.trim());
      corrections = corrections.filter(c => c.id !== id);
      rejectTarget = null;
      rejectReason = '';
      await loadStats();
    } finally { actionLoading = null; }
  }

  function phaseColor(phase: string): string {
    if (phase.includes('scoping')) return '#0284C7';
    if (phase.includes('adapter')) return '#7C3AED';
    return '#64748B';
  }

  function fmtDate(iso: string | null): string {
    if (!iso) return '';
    return new Date(iso).toLocaleDateString('fr-FR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  }

  onMount(async () => {
    await loadStats();
    await loadTab('staging');
  });
</script>

<Topbar title="Mémoire corrective" />

<div class="mem-page">

  <!-- Stats header -->
  <div class="mem-stats-bar">
    <div class="mem-stat">
      <span class="ms-value">{stats?.total ?? '—'}</span>
      <span class="ms-label">corrections total</span>
    </div>
    <div class="mem-stat">
      <span class="ms-value" style="color:#E53E3E">{stagingCount}</span>
      <span class="ms-label">en attente</span>
    </div>
    <div class="mem-chip" class:chip-ok={stats?.kg_available} class:chip-no={!stats?.kg_available}>
      KG {stats?.kg_available ? 'actif' : 'inactif'}
    </div>
    <div class="mem-chip" class:chip-ok={stats?.vector_available} class:chip-no={!stats?.vector_available}>
      Qdrant {stats?.vector_available ? 'actif' : 'inactif'}
    </div>
  </div>

  <!-- Tabs -->
  <div class="mem-tabs">
    <button class="mem-tab" class:active={activeTab === 'staging'} onclick={() => loadTab('staging')}>
      En attente
      {#if stagingCount > 0}<span class="tab-badge">{stagingCount}</span>{/if}
    </button>
    <button class="mem-tab" class:active={activeTab === 'validated'} onclick={() => loadTab('validated')}>
      Validées
    </button>
    <button class="mem-tab" class:active={activeTab === 'rejected'} onclick={() => loadTab('rejected')}>
      Rejetées
    </button>
  </div>

  <!-- List -->
  <div class="mem-list">
    {#if loading}
      <div class="mem-empty">Chargement…</div>
    {:else if corrections.length === 0}
      <div class="mem-empty">
        {activeTab === 'staging' ? 'Aucune correction en attente.' : 'Aucune correction.'}
      </div>
    {:else}
      {#each corrections as c (c.id)}
        <div class="mem-card">
          <div class="mc-header">
            <span class="mc-phase" style="background:{phaseColor(c.phase)}20;color:{phaseColor(c.phase)}">{c.phase}</span>
            <span class="mc-source">{c.source}</span>
            <span class="mc-conf">{Math.round(c.confidence * 100)}% conf.</span>
            <span class="mc-date">{fmtDate(c.created_at)}</span>
          </div>

          <p class="mc-desc">{c.description}</p>

          {#if c.observed_value || c.expected_value}
            <div class="mc-values">
              {#if c.observed_value}<span class="mc-obs">obs : {c.observed_value}</span>{/if}
              {#if c.observed_value && c.expected_value}<span class="mc-arrow">→</span>{/if}
              {#if c.expected_value}<span class="mc-exp">attendu : {c.expected_value}</span>{/if}
            </div>
          {/if}

          {#if c.rationale}
            <p class="mc-rationale">{c.rationale}</p>
          {/if}

          {#if c.context_need || c.context_hw}
            <div class="mc-ctx">
              {#if c.context_hw}<span>matériel : {c.context_hw}</span>{/if}
              {#if c.context_need}<span>besoin : {c.context_need}</span>{/if}
            </div>
          {/if}

          {#if activeTab === 'staging'}
            <div class="mc-actions">
              {#if rejectTarget === c.id}
                <input class="mc-reject-input" placeholder="Raison du rejet *" bind:value={rejectReason} />
                <button class="btn-danger-sm" onclick={() => doReject(c.id)}
                  disabled={actionLoading === c.id || !rejectReason.trim()}>
                  Confirmer
                </button>
                <button class="btn-ghost-sm" onclick={() => { rejectTarget = null; rejectReason = ''; }}>
                  Annuler
                </button>
              {:else}
                <button class="btn-validate" onclick={() => doValidate(c.id)}
                  disabled={actionLoading === c.id}>
                  {actionLoading === c.id ? '…' : 'Valider'}
                </button>
                <button class="btn-reject" onclick={() => rejectTarget = c.id}>
                  Rejeter
                </button>
              {/if}
            </div>
          {/if}

          {#if activeTab === 'validated' && c.validated_by}
            <div class="mc-validated-by">Validé par {c.validated_by} · {fmtDate(c.validated_at)}</div>
          {/if}

          {#if activeTab === 'rejected' && c.rejection_reason}
            <div class="mc-rejection">Rejeté : {c.rejection_reason}</div>
          {/if}
        </div>
      {/each}
    {/if}
  </div>
</div>

<style>
  .mem-page { max-width: 860px; margin: 0 auto; padding: 24px 20px; }

  .mem-stats-bar {
    display: flex; align-items: center; gap: 24px;
    padding: 14px 20px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    margin-bottom: 20px;
  }
  .mem-stat { display: flex; flex-direction: column; gap: 2px; }
  .ms-value { font-size: 20px; font-weight: 700; color: var(--text); line-height: 1; }
  .ms-label { font-size: 11px; color: var(--text-muted); }
  .mem-chip {
    font-size: 11px; font-weight: 600; padding: 3px 8px;
    border-radius: 99px; border: 1px solid;
  }
  .chip-ok  { background: #F0FDF4; color: #16A34A; border-color: #BBF7D0; }
  .chip-no  { background: #FEF2F2; color: #DC2626; border-color: #FECACA; }

  .mem-tabs { display: flex; gap: 4px; margin-bottom: 16px; }
  .mem-tab {
    padding: 7px 16px; font-size: 13px; font-weight: 500;
    background: none; border: 1px solid var(--border);
    border-radius: var(--radius); cursor: pointer; color: var(--text-muted);
    display: flex; align-items: center; gap: 6px; transition: all 120ms;
  }
  .mem-tab:hover { background: var(--surface); color: var(--text); }
  .mem-tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .tab-badge {
    background: #E53E3E; color: #fff;
    font-size: 10px; font-weight: 700;
    padding: 1px 5px; border-radius: 99px;
  }

  .mem-list { display: flex; flex-direction: column; gap: 10px; }
  .mem-empty { padding: 40px; text-align: center; color: var(--text-muted); font-size: 13px; }

  .mem-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 14px 16px;
    display: flex; flex-direction: column; gap: 8px;
  }

  .mc-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .mc-phase {
    font-size: 11px; font-weight: 700; padding: 2px 8px;
    border-radius: 99px;
  }
  .mc-source { font-size: 11px; color: var(--text-muted); }
  .mc-conf   { font-size: 11px; color: var(--text-muted); }
  .mc-date   { font-size: 11px; color: var(--text-muted); margin-left: auto; }

  .mc-desc { font-size: 13px; color: var(--text); margin: 0; line-height: 1.5; }

  .mc-values { display: flex; align-items: center; gap: 8px; }
  .mc-obs  { font-size: 12px; color: #DC2626; background: #FEF2F2; padding: 2px 8px; border-radius: 4px; }
  .mc-exp  { font-size: 12px; color: #16A34A; background: #F0FDF4; padding: 2px 8px; border-radius: 4px; }
  .mc-arrow { font-size: 12px; color: var(--text-muted); }

  .mc-rationale { font-size: 12px; color: var(--text-muted); margin: 0; font-style: italic; }
  .mc-ctx { display: flex; gap: 12px; font-size: 11px; color: var(--text-muted); }

  .mc-actions { display: flex; align-items: center; gap: 8px; padding-top: 4px; }
  .mc-reject-input {
    flex: 1; border: 1px solid var(--border); border-radius: 4px;
    padding: 5px 8px; font-size: 12px; background: var(--surface-2); color: var(--text);
  }
  .btn-validate {
    padding: 5px 14px; font-size: 12px; font-weight: 600;
    background: #16A34A; color: #fff; border: none; border-radius: var(--radius);
    cursor: pointer; transition: opacity 120ms;
  }
  .btn-validate:hover:not(:disabled) { opacity: .85; }
  .btn-validate:disabled { opacity: .5; cursor: not-allowed; }
  .btn-reject {
    padding: 5px 12px; font-size: 12px; font-weight: 500;
    background: none; border: 1px solid #DC2626; color: #DC2626;
    border-radius: var(--radius); cursor: pointer; transition: background 120ms;
  }
  .btn-reject:hover { background: #FEF2F2; }
  .btn-danger-sm {
    padding: 4px 10px; font-size: 12px; font-weight: 600;
    background: #DC2626; color: #fff; border: none;
    border-radius: var(--radius); cursor: pointer;
  }
  .btn-danger-sm:disabled { opacity: .5; cursor: not-allowed; }
  .btn-ghost-sm {
    background: none; border: none; font-size: 12px; color: var(--text-muted);
    cursor: pointer; text-decoration: underline; text-underline-offset: 2px;
  }

  .mc-validated-by { font-size: 11px; color: #16A34A; }
  .mc-rejection    { font-size: 11px; color: #DC2626; }
</style>
