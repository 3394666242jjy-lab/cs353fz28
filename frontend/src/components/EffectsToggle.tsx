import { useEffect, useState } from 'react';

const preferenceKey = 'careerpilot.effects:v1';
export function initializeEffects() {
  let light = navigator.hardwareConcurrency > 0 && navigator.hardwareConcurrency <= 4;
  try {
    const preference = localStorage.getItem(preferenceKey);
    if (preference === 'light' || preference === 'full') light = preference === 'light';
  } catch {
    /* A stored preference is optional. */
  }
  document.documentElement.dataset.effects = light ? 'light' : 'full';
}
export function lightweightEffects() {
  return document.documentElement.dataset.effects === 'light';
}

export default function EffectsToggle() {
  const [light, setLight] = useState(lightweightEffects);
  useEffect(() => {
    const sync = () => setLight(lightweightEffects());
    window.addEventListener('careerpilot-effects-change', sync);
    return () => window.removeEventListener('careerpilot-effects-change', sync);
  }, []);
  return (
    <button
      type="button"
      className="effects-toggle"
      aria-pressed={light}
      title="降低模糊和跟随动效，保留静态玻璃材质"
      onClick={() => {
        const next = !light;
        setLight(next);
        document.documentElement.dataset.effects = next ? 'light' : 'full';
        try {
          localStorage.setItem(preferenceKey, next ? 'light' : 'full');
        } catch {
          /* Optional. */
        }
        window.dispatchEvent(new Event('careerpilot-effects-change'));
      }}
    >
      <span className="effects-state" aria-hidden="true" />
      {light ? '轻量效果已开启' : '轻量效果'}
    </button>
  );
}
