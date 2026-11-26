type IconName =
  'documents' | 'job' | 'analysis' | 'plan' | 'knowledge' | 'arrow' | 'shield' | 'user';

const paths: Record<IconName, string> = {
  documents: 'M7 3h7l4 4v14H7z M14 3v5h4 M4 7v14',
  job: 'M4 7h16v13H4z M9 7V4h6v3 M4 12h16 M10 12v3h4v-3',
  analysis: 'M4 20h16 M7 16v-5 M12 16V5 M17 16V8',
  plan: 'M5 5h15v16H5z M9 3v4 M16 3v4 M8 12l2 2 5-5 M9 18h7',
  knowledge: 'M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1z M12 5v15',
  arrow: 'M5 12h14 M13 6l6 6-6 6',
  shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',
  user: 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M4 21v-3a8 8 0 0 1 16 0v3',
};

export default function Icon({ name }: { name: IconName }) {
  return (
    <svg
      className="ui-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
