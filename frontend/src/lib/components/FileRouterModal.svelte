<script lang="ts">
  interface Props {
    onNorm: () => void;
    onSource: () => void;
    onCancel: () => void;
  }
  let { onNorm, onSource, onCancel }: Props = $props();
</script>

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="overlay" onclick={onCancel}>
  <div class="modal" onclick={(e) => e.stopPropagation()}>
    <div class="modal-title">Ce fichier est…</div>
    <div class="modal-subtitle">Votre réponse détermine comment il sera utilisé.</div>

    <div class="choices">
      <button class="choice" onclick={onNorm}>
        <div class="choice-icon">📋</div>
        <div class="choice-body">
          <div class="choice-name">Une norme ou procédure officielle</div>
          <div class="choice-desc">
            Référentiel, réglementation, procédure interne, standard sectoriel.
          </div>
          <div class="choice-example">
            Ex : norme ISO, procédure de maintenance, réglementation ASN
          </div>
          <div class="choice-dest">→ Contexte sémantique</div>
        </div>
      </button>

      <button class="choice" onclick={onSource}>
        <div class="choice-icon">📂</div>
        <div class="choice-body">
          <div class="choice-name">Un document terrain à traiter</div>
          <div class="choice-desc">
            Cas réels, formulaires remplis, rapports d'intervention, données métier brutes.
          </div>
          <div class="choice-example">
            Ex : demandes d'intervention, rapports de panne, comptes-rendus terrain
          </div>
          <div class="choice-dest">→ Ingestion de données</div>
        </div>
      </button>
    </div>

    <button class="btn-cancel" onclick={onCancel}>Annuler</button>
  </div>
</div>

<style>
  .overlay {
    position: fixed; inset: 0;
    background: rgba(15, 23, 42, 0.5);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: #fff; border-radius: 14px;
    padding: 32px; width: 560px; max-width: 95vw;
    box-shadow: 0 20px 60px rgba(0,0,0,.18);
  }

  .modal-title {
    font-size: 18px; font-weight: 700; color: #0F172A;
    margin-bottom: 6px;
  }
  .modal-subtitle {
    font-size: 13px; color: #64748B; margin-bottom: 24px;
  }

  .choices { display: flex; flex-direction: column; gap: 12px; }

  .choice {
    display: flex; gap: 16px; align-items: flex-start;
    padding: 18px; border: 2px solid #E2E8F0; border-radius: 10px;
    background: #fff; cursor: pointer; text-align: left;
    transition: border-color 0.15s, background 0.15s;
    width: 100%;
  }
  .choice:hover { border-color: #0284C7; background: #F0F9FF; }

  .choice-icon { font-size: 24px; flex-shrink: 0; margin-top: 2px; }

  .choice-name {
    font-size: 14px; font-weight: 700; color: #0F172A; margin-bottom: 4px;
  }
  .choice-desc {
    font-size: 12px; color: #475569; line-height: 1.5; margin-bottom: 6px;
  }
  .choice-example {
    font-size: 11px; color: #94A3B8; font-style: italic; margin-bottom: 6px;
  }
  .choice-dest {
    font-size: 12px; font-weight: 600; color: #0284C7;
  }

  .btn-cancel {
    margin-top: 20px; width: 100%;
    padding: 10px; background: none;
    border: 1px solid #E2E8F0; border-radius: 8px;
    font-size: 13px; color: #64748B; cursor: pointer;
  }
  .btn-cancel:hover { background: #F8FAFC; }
</style>
