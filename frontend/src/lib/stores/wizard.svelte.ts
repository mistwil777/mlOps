/**
 * Shared state between the Advisor page and the /start wizard.
 * The advisor writes here before navigating; the wizard reads on mount.
 */
import type { AdvisorAdapter } from '$lib/types';

export const wizardHandoff = $state<{
  need: string;
  adapters: AdvisorAdapter[];
  orchestrator: AdvisorAdapter | null;
}>({
  need: '',
  adapters: [],
  orchestrator: null,
});

export function setWizardHandoff(
  need: string,
  adapters: AdvisorAdapter[],
  orchestrator: AdvisorAdapter | null,
) {
  wizardHandoff.need = need;
  wizardHandoff.adapters = [...adapters];
  wizardHandoff.orchestrator = orchestrator ? { ...orchestrator } : null;
}

export function clearWizardHandoff() {
  wizardHandoff.need = '';
  wizardHandoff.adapters = [];
  wizardHandoff.orchestrator = null;
}
