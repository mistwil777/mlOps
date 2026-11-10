export interface DocumentItem {
  label: string;
  category: 'norme' | 'pattern' | 'log';
  source?: string;
}

export interface DocumentChecklist {
  normes: DocumentItem[];
  patterns: DocumentItem[];
  logs: DocumentItem[];
  fromCdc: boolean;
}

// Strict norm codes only: must be PREFIX-DIGITS or PREFIX DIGITS (e.g. DC-198, ISO 9001, IEC 61508)
const NORM_CODE_RE = /\b(?:DC|RCC|ISO|IEC|NF|IEEE|ASME|ASTM|NFPA|CEI|UNE|DIN|VDA|IATF|DO-|SAE|MIL-|STANAG)[-\s]\d[\w\d\-]*/g;

function extractNormCodes(text: string): string[] {
  const matches = text.match(NORM_CODE_RE) ?? [];
  return [...new Set(matches.map(m => m.trim()))];
}

function scanAllTextFields(cfg: Record<string, unknown>): string {
  const collect = (v: unknown): string => {
    if (typeof v === 'string') return v;
    if (Array.isArray(v)) return v.map(collect).join(' ');
    if (v && typeof v === 'object') return Object.values(v as object).map(collect).join(' ');
    return '';
  };
  return collect(cfg);
}

export function buildChecklist(cfg: Record<string, unknown>): DocumentChecklist {
  // 1. Structured key: documents: { normes: [], patterns: [], logs: [] }
  const docs = cfg.documents as Record<string, unknown> | undefined;
  if (docs) {
    const toItems = (arr: unknown, cat: DocumentItem['category']): DocumentItem[] =>
      Array.isArray(arr)
        ? arr.map(v => ({ label: String(v), category: cat }))
        : [];
    return {
      normes:   toItems(docs.normes,   'norme'),
      patterns: toItems(docs.patterns, 'pattern'),
      logs:     toItems(docs.logs,     'log'),
      fromCdc: true,
    };
  }

  // 2. Regex scan of all text fields
  const allText = scanAllTextFields(cfg);
  const codes = extractNormCodes(allText);
  if (codes.length > 0) {
    return {
      normes:   codes.map(c => ({ label: c, category: 'norme' as const })),
      patterns: [],
      logs:     [],
      fromCdc: true,
    };
  }

  // 3. Generic fallback
  return {
    normes:   [{ label: 'Normes et référentiels sectoriels', category: 'norme' }],
    patterns: [{ label: 'Procédures et référentiels internes', category: 'pattern' }],
    logs:     [{ label: 'Historique de feedback et logs terrain', category: 'log' }],
    fromCdc: false,
  };
}
