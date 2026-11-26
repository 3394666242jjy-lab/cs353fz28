import { useEffect, useRef, type ReactNode, type RefObject } from 'react';

export default function ReadingDialog({
  title,
  onClose,
  trigger,
  children,
}: {
  title: string;
  onClose: () => void;
  trigger: RefObject<HTMLButtonElement | null>;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = dialog.current;
    if (!node) return;
    const opener = trigger.current;
    const previousOverflow = document.body.style.overflow;
    node.showModal();
    document.body.style.overflow = 'hidden';
    return () => {
      node.close();
      document.body.style.overflow = previousOverflow;
      opener?.focus({ preventScroll: true });
    };
  }, [trigger]);
  return (
    <dialog
      ref={dialog}
      className="reading-dialog"
      aria-label={`专注阅读：${title}`}
      onKeyDown={(event) => {
        if (event.key !== 'Tab') return;
        const elements = Array.from(
          event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not([disabled]), a[href], summary, [tabindex="0"]',
          ),
        ).filter((element) => element.getClientRects().length > 0);
        const first = elements[0];
        const last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          const bounds = event.currentTarget.getBoundingClientRect();
          if (
            event.clientX < bounds.left ||
            event.clientX > bounds.right ||
            event.clientY < bounds.top ||
            event.clientY > bounds.bottom
          )
            onClose();
        }
      }}
    >
      <header className="reading-dialog-toolbar">
        <span>专注阅读</span>
        <button type="button" className="secondary-button" autoFocus onClick={onClose}>
          退出专注阅读 <span aria-hidden="true">×</span>
        </button>
      </header>
      <div className="reading-dialog-body">{children}</div>
    </dialog>
  );
}
