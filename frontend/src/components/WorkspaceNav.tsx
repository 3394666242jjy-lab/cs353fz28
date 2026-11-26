import { useLayoutEffect, useRef } from 'react';
import Icon from './Icon';

const items = [
  { label: '资料准备', icon: 'documents' },
  { label: '目标岗位', icon: 'job' },
  { label: '匹配分析', icon: 'analysis' },
  { label: '准备计划', icon: 'plan' },
  { label: '知识素材', icon: 'knowledge' },
] as const;

export default function WorkspaceNav({
  step,
  onSelect,
}: {
  step: number;
  onSelect: (step: number) => void;
}) {
  const nav = useRef<HTMLElement>(null);
  const plate = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const node = nav.current;
    const light = plate.current;
    if (!node || !light) return;
    const update = () => {
      const active = node.querySelector<HTMLElement>('[aria-current="page"]');
      if (!active) return;
      const { offsetLeft: x, offsetTop: y, offsetWidth: width, offsetHeight: height } = active;
      light.style.width = `${width}px`;
      light.style.height = `${height}px`;
      light.style.transform = `translate3d(${x}px, ${y}px, 0)`;
      light.dataset.ready = 'true';
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    for (const button of node.querySelectorAll('button')) observer.observe(button);
    document.fonts?.addEventListener('loadingdone', update);
    return () => {
      observer.disconnect();
      document.fonts?.removeEventListener('loadingdone', update);
    };
  }, [step]);
  return (
    <nav ref={nav} aria-label="准备步骤" className="step-nav sliding-nav">
      <span ref={plate} className="nav-glass-plate" aria-hidden="true" />
      {items.map((item, index) => (
        <button
          key={item.label}
          type="button"
          className={`nav-item ${step === index ? 'active' : ''}`}
          aria-current={step === index ? 'page' : undefined}
          onClick={() => onSelect(index)}
        >
          <Icon name={item.icon} />
          <span>{item.label}</span>
          {step === index && <span className="nav-indicator" aria-hidden="true" />}
        </button>
      ))}
    </nav>
  );
}
