import { useEffect, useRef } from 'react';
import { lightweightEffects } from './EffectsToggle';

/** A single composited reflection shared by the workspace, with no pointer-driven React renders. */
export default function WorkspaceLight({ scene }: { scene: number }) {
  const reflection = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const light = reflection.current;
    const workspace = light?.closest('.glass-workspace');
    if (!light || !workspace) return;
    const motion = window.matchMedia(
      '(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)',
    );
    let frame: number | null = null;
    let point = { x: 0, y: 0 };
    const hide = () => {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      light.removeAttribute('data-visible');
    };
    const move = (event: Event) => {
      const pointer = event as PointerEvent;
      if (!motion.matches || lightweightEffects() || pointer.pointerType !== 'mouse') {
        hide();
        return;
      }
      point = { x: pointer.clientX, y: pointer.clientY };
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        light.style.transform = `translate3d(${point.x - 120}px, ${point.y - 90}px, 0)`;
        light.setAttribute('data-visible', 'true');
      });
    };
    workspace.addEventListener('pointermove', move, { passive: true });
    workspace.addEventListener('pointerleave', hide);
    workspace.addEventListener('pointercancel', hide);
    document.addEventListener('visibilitychange', hide);
    window.addEventListener('blur', hide);
    window.addEventListener('scroll', hide, { capture: true, passive: true });
    window.addEventListener('resize', hide);
    window.addEventListener('careerpilot-effects-change', hide);
    motion.addEventListener('change', hide);
    return () => {
      hide();
      workspace.removeEventListener('pointermove', move);
      workspace.removeEventListener('pointerleave', hide);
      workspace.removeEventListener('pointercancel', hide);
      document.removeEventListener('visibilitychange', hide);
      window.removeEventListener('blur', hide);
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('resize', hide);
      window.removeEventListener('careerpilot-effects-change', hide);
      motion.removeEventListener('change', hide);
    };
  }, [scene]);

  return (
    <span className="workspace-light-field" aria-hidden="true">
      <span ref={reflection} className="workspace-reflection" />
    </span>
  );
}
