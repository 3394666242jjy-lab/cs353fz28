import { useEffect, useRef, type KeyboardEvent, type PointerEvent } from 'react';
import Icon from './Icon';
import { glassWords, readingInfluence } from './glass-reading';
import { lightweightEffects } from './EffectsToggle';

interface GlassReaderProps {
  active: boolean;
  filename: string;
  topic: string;
  quote: string;
  locator: string;
  version: number;
  analysis?: boolean;
  onFlip: () => void;
}

function GlassText({ text }: { text: string }) {
  return (
    <>
      <span className="dock-accessible">{text}</span>
      <span className="dock-visual" aria-hidden="true">
        {glassWords(text).map((word, index) =>
          /\S/u.test(word) ? (
            <span className="dock-word" key={index}>
              {word}
            </span>
          ) : (
            word
          ),
        )}
      </span>
    </>
  );
}

type WordBox = { element: HTMLElement; x: number; y: number; strength: number };
type Layout = { bounds: DOMRect; words: WordBox[] };

export default function GlassReader(props: GlassReaderProps) {
  const surface = useRef<HTMLDivElement>(null);
  const layout = useRef<Layout | null>(null);
  const frame = useRef<number | null>(null);
  const active = useRef(props.active);
  const target = useRef<
    { kind: 'pointer'; x: number; y: number } | { kind: 'word'; index: number } | null
  >(null);
  const keyIndex = useRef(0);
  active.current = props.active;

  function reset() {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    target.current = null;
    for (const word of layout.current?.words ?? []) {
      if (word.strength !== 0) word.element.style.removeProperty('--word-focus');
      word.strength = 0;
    }
    surface.current?.removeAttribute('data-reading');
  }
  function measure(): Layout | null {
    const node = surface.current;
    if (!node) return null;
    // Read static offsets, never the animated word bounds. Batch all reads before writes.
    const bounds = node.getBoundingClientRect();
    const words = Array.from(node.querySelectorAll<HTMLElement>('.dock-word')).map((element) => {
      let x = element.offsetWidth / 2;
      let y = element.offsetHeight / 2;
      let ancestor: HTMLElement | null = element;
      while (ancestor && ancestor !== node) {
        x += ancestor.offsetLeft;
        y += ancestor.offsetTop;
        ancestor = ancestor.offsetParent as HTMLElement | null;
      }
      return { element, x, y, strength: 0 };
    });
    return { bounds, words };
  }
  function schedule() {
    if (frame.current !== null || !active.current || lightweightEffects()) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      if (!active.current || !target.current) return;
      const current = layout.current ?? measure();
      if (!current || !current.words.length) return;
      layout.current = current;
      const input = target.current;
      const selected = current.words[Math.min(keyIndex.current, current.words.length - 1)];
      const x = input.kind === 'pointer' ? input.x - current.bounds.left : selected.x;
      const y = input.kind === 'pointer' ? input.y - current.bounds.top : selected.y;
      for (const word of current.words) {
        const strength = readingInfluence(word.x - x, word.y - y);
        if (Math.abs(strength - word.strength) < 0.006 && !(strength === 0 && word.strength !== 0))
          continue;
        word.element.style.setProperty('--word-focus', strength.toFixed(3));
        word.strength = strength;
      }
      surface.current?.setAttribute('data-reading', '');
    });
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    if (!active.current) return;
    target.current = { kind: 'pointer', x: event.clientX, y: event.clientY };
    schedule();
  }
  function key(event: KeyboardEvent<HTMLDivElement>) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      reset();
      props.onFlip();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      reset();
      return;
    }
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key))
      return;
    event.preventDefault();
    const current = layout.current ?? measure();
    if (!current || !current.words.length) return;
    layout.current = current;
    keyIndex.current =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? current.words.length - 1
          : Math.max(
              0,
              Math.min(
                current.words.length - 1,
                keyIndex.current + (event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1),
              ),
            );
    target.current = { kind: 'word', index: keyIndex.current };
    schedule();
  }
  useEffect(() => {
    reset();
    layout.current = null;
    keyIndex.current = 0;
  }, [props.active, props.topic, props.quote]);
  useEffect(() => {
    const invalidate = () => {
      reset();
      layout.current = null;
    };
    const resize = new ResizeObserver(invalidate);
    if (surface.current) resize.observe(surface.current);
    window.addEventListener('scroll', invalidate, { capture: true, passive: true });
    window.addEventListener('resize', invalidate);
    window.addEventListener('careerpilot-effects-change', invalidate);
    document.fonts?.addEventListener('loadingdone', invalidate);
    return () => {
      reset();
      resize.disconnect();
      window.removeEventListener('scroll', invalidate, true);
      window.removeEventListener('resize', invalidate);
      window.removeEventListener('careerpilot-effects-change', invalidate);
      document.fonts?.removeEventListener('loadingdone', invalidate);
    };
  }, []);
  return (
    <div
      ref={surface}
      className="glass-reading-surface"
      role="group"
      aria-label={
        props.analysis
          ? '分析结论阅读，方向键逐字聚焦，Enter 翻回原文'
          : '项目原文阅读，方向键逐字聚焦，Enter 翻面核对'
      }
      tabIndex={props.active ? 0 : -1}
      onPointerEnter={() => {
        reset();
        layout.current = null;
      }}
      onPointerMove={move}
      onPointerDown={move}
      onPointerCancel={reset}
      onPointerLeave={reset}
      onBlur={reset}
      onKeyDown={key}
      onFocus={() => {
        if (!surface.current?.matches(':focus-visible')) return;
        keyIndex.current = 0;
        target.current = { kind: 'word', index: 0 };
        schedule();
      }}
    >
      <div className="reader-page">
        <span className="flip-label">
          <Icon name={props.analysis ? 'shield' : 'documents'} />
          {props.analysis ? '分析结论 · 合成样例' : props.filename}
          <small>v{props.version}</small>
        </span>
        <h3>
          <GlassText text={props.topic} />
        </h3>
        <p>
          <GlassText text={props.quote} />
        </p>
        <span className="flip-bottom">
          <span>{props.locator}</span>
          <span>{props.analysis ? '结论与出处，放在一起' : '轻触或移动，细读原文'}</span>
        </span>
      </div>
    </div>
  );
}
