import catalog from '../../../knowledge/sources.json';

export const roles = {
  ai_product_manager: { label: 'AI 产品经理', description: '从用户需求到指标与评价', symbol: '◈' },
  fullstack: { label: '全栈开发', description: '连接界面、接口与数据', symbol: '⌘' },
  agent_engineer: { label: 'Agent 开发', description: '理解工具、检索与恢复', symbol: '◇' },
} as const;
export type Role = keyof typeof roles;

const cards = import.meta.glob('../../../knowledge/cards/*.md', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

export const knowledgeSources = catalog.sources.map((source) => {
  const content = cards[`../../../knowledge/${source.local_path}`];
  if (!content) throw new Error(`Missing knowledge card: ${source.id}`);
  return { ...source, content };
});

export function filterKnowledge(role: Role, query: string, topic: string) {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return knowledgeSources.filter(
    (source) =>
      source.roles.includes(role) &&
      (!topic || source.topics.includes(topic)) &&
      terms.every((term) =>
        `${source.title} ${source.topics.join(' ')} ${source.content}`
          .toLocaleLowerCase()
          .includes(term),
      ),
  );
}
