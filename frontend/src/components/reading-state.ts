export interface ReadingState {
  role: 'ai_product_manager' | 'fullstack' | 'agent_engineer';
  query: string;
  topic: string;
  selectedId: string | null;
  pageY: number;
  listY: number;
}

export const emptyReadingState: ReadingState = {
  role: 'ai_product_manager',
  query: '',
  topic: '',
  selectedId: null,
  pageY: 0,
  listY: 0,
};

export function parseReadingState(raw: string | null): ReadingState {
  try {
    const value = JSON.parse(raw ?? 'null');
    if (!value || value.version !== 1) return { ...emptyReadingState };
    return {
      role: ['ai_product_manager', 'fullstack', 'agent_engineer'].includes(value.role)
        ? value.role
        : emptyReadingState.role,
      query: typeof value.query === 'string' ? value.query.slice(0, 300) : '',
      topic: typeof value.topic === 'string' ? value.topic.slice(0, 100) : '',
      selectedId: typeof value.selectedId === 'string' ? value.selectedId.slice(0, 200) : null,
      pageY: Number.isFinite(value.pageY) && value.pageY > 0 ? Math.min(value.pageY, 100000) : 0,
      listY: Number.isFinite(value.listY) && value.listY > 0 ? Math.min(value.listY, 100000) : 0,
    };
  } catch {
    return { ...emptyReadingState };
  }
}

/** Literal matching: punctuation such as C++ and [API] is never interpreted as a regex. */
export function highlightParts(text: string, query: string): { text: string; match: boolean }[] {
  const haystack = text.toLocaleLowerCase();
  const ranges: [number, number][] = [];
  for (const term of new Set(query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean))) {
    let index = haystack.indexOf(term);
    while (index !== -1) {
      ranges.push([index, index + term.length]);
      index = haystack.indexOf(term, index + term.length);
    }
  }
  ranges.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const range of ranges) {
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([...range]);
  }
  const parts: { text: string; match: boolean }[] = [];
  let index = 0;
  for (const [start, end] of merged) {
    if (start > index) parts.push({ text: text.slice(index, start), match: false });
    parts.push({ text: text.slice(start, end), match: true });
    index = end;
  }
  if (index < text.length) parts.push({ text: text.slice(index), match: false });
  return parts;
}
