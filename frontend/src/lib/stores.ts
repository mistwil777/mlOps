import { writable, derived } from 'svelte/store';
import type { AdvisorAdapter, AdvisorRecommendation, SelectionEval } from './types';

interface AdvisorState {
  need: string;
  model: string;
  result: AdvisorRecommendation | null;
  adapters: AdvisorAdapter[];
  orchestrator: AdvisorAdapter | null;
  selEval: SelectionEval | null;
}

function createAdvisorStore() {
  const initial: AdvisorState = {
    need: '',
    model: 'us.anthropic.claude-sonnet-4-6',
    result: null,
    adapters: [],
    orchestrator: null,
    selEval: null,
  };

  const { subscribe, set, update } = writable<AdvisorState>(initial);

  return {
    subscribe,
    setNeed: (need: string) => update(s => ({ ...s, need })),
    setModel: (model: string) => update(s => ({ ...s, model })),
    setResult: (result: AdvisorRecommendation) =>
      update(s => ({
        ...s,
        result,
        adapters: [...result.recommendation.adapters],
        orchestrator: { ...result.recommendation.orchestrator },
        selEval: null,
      })),
    setAdapters: (adapters: AdvisorAdapter[]) => update(s => ({ ...s, adapters })),
    setOrchestrator: (orchestrator: AdvisorAdapter | null) => update(s => ({ ...s, orchestrator })),
    setSelEval: (selEval: SelectionEval | null) => update(s => ({ ...s, selEval })),
    reset: () => set(initial),
  };
}

export const advisorStore = createAdvisorStore();

export const advisorSelection = derived(advisorStore, $s =>
  [...$s.adapters, ...($s.orchestrator ? [$s.orchestrator] : [])]
);
