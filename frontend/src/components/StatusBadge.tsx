export default function StatusBadge({
  label,
  tone = 'neutral',
}: {
  label: string;
  tone?: 'neutral' | 'success' | 'warning' | 'danger';
}) {
  return (
    <span className={`badge badge-${tone}`}>
      <span aria-hidden="true" className="status-dot" />
      {label}
    </span>
  );
}
