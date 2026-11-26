const segmenter = new Intl.Segmenter('zh', { granularity: 'grapheme' });

export function glassWords(text: string): string[] {
  return Array.from(segmenter.segment(text), (part) => part.segment);
}

/** Compact, continuous Dock-like falloff. No movement outside the reading neighbourhood. */
export function readingInfluence(dx: number, dy: number): number {
  const distance = (dx / 94) ** 2 + (dy / 38) ** 2;
  if (distance >= 1) return 0;
  return (1 - distance) ** 2;
}
