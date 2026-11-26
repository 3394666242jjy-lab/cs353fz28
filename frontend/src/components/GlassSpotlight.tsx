import { useEffect, useRef, useState, type PointerEvent, type MouseEvent } from 'react';
import { sampleAnalysis, sampleEvidence } from '../api/fixtures';
import Icon from './Icon';
import GlassReader from './GlassReader';
import { lightweightEffects } from './EffectsToggle';

const topics = ['数据建模', '缓存实践'];

export default function GlassSpotlight() {
  const [open, setOpen] = useState(false);
  const [material, setMaterial] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [turning, setTurning] = useState(false);
  const stageRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const flipActionRef = useRef<HTMLButtonElement>(null);
  const frameRef = useRef<number | null>(null);
  const reflectionRef = useRef<HTMLSpanElement>(null);
  const lightPoint = useRef({ x: 0, y: 0 });
  const restoreFocusRef = useRef<number | null>(null);
  const evidence = sampleEvidence[material];
  const match = sampleAnalysis.matches.find((item) =>
    item.evidence_refs.some((ref) => ref.chunk_id === evidence.chunk_id),
  );
  const locator =
    evidence.locator.kind === 'text'
      ? evidence.locator.line_start === evidence.locator.line_end
        ? `第 ${evidence.locator.line_start} 行`
        : `第 ${evidence.locator.line_start}–${evidence.locator.line_end} 行`
      : `第 ${evidence.locator.page} 页`;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (open && dialog && !dialog.open) dialog.showModal();
    if (!open && dialog?.open) dialog.close();
  }, [open]);
  useEffect(
    () => () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      if (restoreFocusRef.current !== null) cancelAnimationFrame(restoreFocusRef.current);
    },
    [],
  );

  function close() {
    setOpen(false);
    restoreFocusRef.current = requestAnimationFrame(() => triggerRef.current?.focus());
  }
  function openOriginal(event: MouseEvent<HTMLButtonElement>) {
    triggerRef.current = event.currentTarget;
    setOpen(true);
  }
  function flip() {
    if (turning) return;
    flipActionRef.current?.focus({ preventScroll: true });
    setTurning(!window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    setFlipped((current) => !current);
  }
  useEffect(() => {
    if (!turning) return;
    const timer = window.setTimeout(() => setTurning(false), 720);
    return () => window.clearTimeout(timer);
  }, [turning, flipped]);
  function move(event: PointerEvent<HTMLDivElement>) {
    if (
      event.pointerType !== 'mouse' ||
      lightweightEffects() ||
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    )
      return;
    const rect = event.currentTarget.getBoundingClientRect();
    lightPoint.current = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    if (frameRef.current !== null) return;
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null;
      if (!reflectionRef.current) return;
      reflectionRef.current.style.transform = `translate3d(${lightPoint.current.x - 180}px, ${lightPoint.current.y - 130}px, 0)`;
      reflectionRef.current.dataset.visible = 'true';
    });
  }

  return (
    <div className="spotlight-wrap material-showcase">
      <div className="material-backglass" aria-hidden="true">
        <i />
        <i />
      </div>
      <div
        ref={stageRef}
        className="glass-spotlight material-hybrid"
        onPointerMove={move}
        onPointerLeave={() => {
          if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
          frameRef.current = null;
          reflectionRef.current?.removeAttribute('data-visible');
        }}
      >
        <span ref={reflectionRef} className="material-reflection" aria-hidden="true" />
        <div className="spotlight-caption">
          <Icon name="documents" />
          项目经历预览<span>合成样例</span>
        </div>
        <div className="material-topic-picker" role="group" aria-label="选择项目片段">
          {topics.map((topic, index) => (
            <button
              key={topic}
              type="button"
              aria-pressed={material === index}
              onClick={() => {
                setMaterial(index);
                setFlipped(false);
                setTurning(
                  flipped && !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
                );
              }}
            >
              {topic}
            </button>
          ))}
          <span>
            {material + 1} / {sampleEvidence.length}
          </span>
        </div>
        <div className="glass-flip-stage">
          <div
            className={`glass-flip ${flipped ? 'is-flipped' : ''}`}
            onTransitionEnd={(event) => {
              if (event.target === event.currentTarget && event.propertyName === 'transform')
                setTurning(false);
            }}
          >
            <div className="flip-face flip-front" aria-hidden={flipped} inert={flipped}>
              <GlassReader
                active={!flipped && !turning}
                filename={evidence.filename}
                topic={topics[material]}
                quote={evidence.quote}
                locator={locator}
                version={evidence.version}
                onFlip={flip}
              />
            </div>
            <div className="flip-face flip-back" aria-hidden={!flipped} inert={!flipped}>
              <GlassReader
                active={flipped && !turning}
                filename={evidence.filename}
                topic={match?.status === 'supported' ? '有原文支持' : '还缺测量依据'}
                quote={match?.explanation ?? ''}
                locator={evidence.filename}
                version={evidence.version}
                onFlip={flip}
                analysis
              />
            </div>
          </div>
        </div>
        <div className="material-story">
          <h2>一面经历，一面结论。</h2>
          <p>
            {flipped ? '靠近文字，细读结论；翻回原文核对。' : '靠近文字，细读经历；翻面核对结论。'}
          </p>
        </div>
        <div className="material-footer">
          <button
            type="button"
            ref={flipActionRef}
            className="material-open material-flip-action"
            onClick={flip}
            aria-disabled={turning}
            aria-label={flipped ? '翻回项目原文' : '翻面查看分析结论'}
          >
            <Icon name="analysis" />
            {flipped ? '翻回原文' : '翻面核对'}
          </button>
          <button
            type="button"
            className="text-button material-source-action"
            onClick={openOriginal}
            aria-haspopup="dialog"
          >
            打开完整原文
            <Icon name="arrow" />
          </button>
        </div>
      </div>
      <dialog
        ref={dialogRef}
        className="glass-focus-dialog"
        aria-labelledby="focus-title"
        onCancel={(event) => {
          event.preventDefault();
          close();
        }}
        onClose={() => setOpen(false)}
        onKeyDown={(event) => {
          if (event.key !== 'Tab') return;
          const controls =
            event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
          const first = controls[0];
          const last = controls[controls.length - 1];
          if (!first || !last) return;
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }}
        onClick={(event) => {
          if (event.target === event.currentTarget) {
            const rect = event.currentTarget.getBoundingClientRect();
            if (
              event.clientX < rect.left ||
              event.clientX > rect.right ||
              event.clientY < rect.top ||
              event.clientY > rect.bottom
            )
              close();
          }
        }}
      >
        <div className="focus-dialog-header">
          <span>
            <Icon name="shield" />
            原文特写
          </span>
          <button className="glass-icon-button" autoFocus onClick={close} aria-label="关闭原文特写">
            ×
          </button>
        </div>
        <div className="focus-dialog-content">
          <span className="focus-kicker">合成项目说明</span>
          <h2 id="focus-title">让结论，回到原文。</h2>
          <div className="focus-source">
            {evidence.filename}
            <span>
              版本 {evidence.version} ·{' '}
              {evidence.locator.kind === 'text'
                ? `第 ${evidence.locator.line_start}–${evidence.locator.line_end} 行`
                : ''}
            </span>
          </div>
          <blockquote key={material}>{evidence.quote}</blockquote>
          <div className="focus-switcher" role="group" aria-label="选择原文片段">
            <button aria-pressed={material === 0} onClick={() => setMaterial(0)}>
              数据库设计
            </button>
            <button aria-pressed={material === 1} onClick={() => setMaterial(1)}>
              缓存与性能
            </button>
          </div>
          <p className="focus-note">合成样例，用于展示引用交互；不会作为你的个人经历。</p>
        </div>
      </dialog>
    </div>
  );
}
