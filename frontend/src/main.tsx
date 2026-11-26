import React, { lazy, Suspense } from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { initializeEffects } from './components/EffectsToggle';
import type { DesignDirection } from './DesignPreview';
import './styles.css';
import './theme.css';
import './glass.css';

const direction = new URLSearchParams(window.location.search).get('design');
initializeEffects();
const DesignPreview = import.meta.env.DEV ? lazy(() => import('./DesignPreview')) : null;

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {DesignPreview && direction && ['aurora', 'orbital', 'glass'].includes(direction) ? (
      <Suspense fallback={<p className="preview-loading">正在打开设计预览…</p>}>
        <DesignPreview initialDirection={direction as DesignDirection} />
      </Suspense>
    ) : (
      <App />
    )}
  </React.StrictMode>,
);
