import { useEffect, useRef, type CSSProperties } from 'react';
import { roles, type Role } from '../api/knowledge';
import { lightweightEffects } from './EffectsToggle';

const roleKeys = Object.keys(roles) as Role[];

export default function RolePicker({
  value,
  onChange,
  label = '岗位方向',
  counts,
}: {
  value: Role;
  onChange: (role: Role) => void;
  label?: string;
  counts?: Partial<Record<Role, number>>;
}) {
  const group = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = group.current;
    if (!node) return;
    const motion = window.matchMedia('(prefers-reduced-motion: no-preference)');
    const pointer = window.matchMedia('(hover: hover) and (pointer: fine)');
    let frame: number | null = null;
    let current: HTMLButtonElement | null = null;
    let box: { left: number; top: number; width: number; height: number } | null = null;
    let point = { x: 0, y: 0 };
    const animations = new Set<Animation>();

    const reset = () => {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      if (current) {
        current.removeAttribute('data-hover');
        for (const property of ['--tilt-x', '--tilt-y', '--glare-shift', '--glare-angle']) {
          current.style.removeProperty(property);
        }
      }
      current = null;
      box = null;
    };
    const stop = () => {
      reset();
      for (const animation of animations) animation.cancel();
      animations.clear();
    };
    const cardAt = (target: EventTarget | null) => {
      const card =
        target instanceof Element ? target.closest<HTMLButtonElement>('.role-option') : null;
      return card?.parentElement === node ? card : null;
    };
    const move = (event: PointerEvent) => {
      if (
        !motion.matches ||
        lightweightEffects() ||
        !pointer.matches ||
        event.pointerType !== 'mouse'
      )
        return;
      const card = cardAt(event.target);
      if (!card) {
        reset();
        return;
      }
      if (current !== card) {
        reset();
        // Read the stationary group and untransformed offsets, never animated card bounds.
        const parent = node.getBoundingClientRect();
        box = {
          left: parent.left + card.offsetLeft,
          top: parent.top + card.offsetTop,
          width: card.offsetWidth,
          height: card.offsetHeight,
        };
        current = card;
      }
      point = { x: event.clientX, y: event.clientY };
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        if (!current || !box) return;
        const x = Math.max(-1, Math.min(1, ((point.x - box.left) / box.width) * 2 - 1));
        const y = Math.max(-1, Math.min(1, ((point.y - box.top) / box.height) * 2 - 1));
        current.style.setProperty('--tilt-x', `${(-y * 2.4).toFixed(2)}deg`);
        current.style.setProperty('--tilt-y', `${(x * 2.4).toFixed(2)}deg`);
        current.style.setProperty('--glare-shift', `${(x * 32).toFixed(2)}%`);
        current.style.setProperty('--glare-angle', `${(-16 + y * 8).toFixed(2)}deg`);
        current.setAttribute('data-hover', 'true');
      });
    };
    const confirm = (event: MouseEvent) => {
      const card = cardAt(event.target);
      if (!card || !motion.matches || lightweightEffects()) return;
      // A click also covers keyboard activation and touch; business selection never waits.
      for (const animation of animations) animation.cancel();
      animations.clear();
      const animation = card.animate(
        [{ scale: '0.985' }, { scale: '1.008', offset: 0.65 }, { scale: '1' }],
        { duration: 360, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' },
      );
      animations.add(animation);
      const release = () => animations.delete(animation);
      animation.onfinish = release;
      animation.oncancel = release;
    };
    node.addEventListener('pointermove', move, { passive: true });
    node.addEventListener('pointerleave', reset);
    node.addEventListener('pointercancel', stop);
    node.addEventListener('click', confirm);
    node.addEventListener('keydown', reset);
    document.addEventListener('visibilitychange', stop);
    window.addEventListener('scroll', stop, { capture: true, passive: true });
    window.addEventListener('blur', stop);
    window.addEventListener('resize', stop);
    window.addEventListener('careerpilot-effects-change', stop);
    motion.addEventListener('change', stop);
    pointer.addEventListener('change', stop);
    const resize = new ResizeObserver(stop);
    resize.observe(node);
    return () => {
      stop();
      resize.disconnect();
      node.removeEventListener('pointermove', move);
      node.removeEventListener('pointerleave', reset);
      node.removeEventListener('pointercancel', stop);
      node.removeEventListener('click', confirm);
      node.removeEventListener('keydown', reset);
      document.removeEventListener('visibilitychange', stop);
      window.removeEventListener('scroll', stop, true);
      window.removeEventListener('blur', stop);
      window.removeEventListener('resize', stop);
      window.removeEventListener('careerpilot-effects-change', stop);
      motion.removeEventListener('change', stop);
      pointer.removeEventListener('change', stop);
    };
  }, []);

  return (
    <div
      ref={group}
      className="role-picker role-picker-interactive"
      role="group"
      aria-label={label}
      style={{ '--role-index': roleKeys.indexOf(value) } as CSSProperties}
    >
      <span className="role-selection-light" aria-hidden="true" />
      {roleKeys.map((key) => (
        <button
          key={key}
          type="button"
          className={`role-option ${value === key ? 'active' : ''}`}
          aria-pressed={value === key}
          onClick={() => onChange(key)}
        >
          <span className="role-symbol" aria-hidden="true">
            {roles[key].symbol}
          </span>
          <strong>{roles[key].label}</strong>
          <span>{roles[key].description}</span>
          {counts?.[key] !== undefined && <small>{counts[key]} 份素材</small>}
        </button>
      ))}
    </div>
  );
}
