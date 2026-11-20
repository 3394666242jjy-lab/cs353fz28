/** Consumer contract for PR #42, commit 25ff5d4 (not yet merged into main).
 * Keep generated OpenAPI files owned by the backend exporter; replace these
 * aliases with generated schema types when that backend contract lands.
 */
import type { Role } from './knowledge';

export interface KnowledgeSource {
  id: string;
  title: string;
  url: string;
  roles: Role[];
  topics: string[];
  version: string;
  verified_on: string;
  content_kind: string;
  usage: string;
}

export interface KnowledgeReference {
  source_id: string;
  source_version: string;
  source_url: string;
  title: string;
  summary: string;
  roles: Role[];
  content_kind: 'editorial_summary';
  usage: 'reference_only';
}

export interface KnowledgeQuery {
  role: Role;
  query: string;
  top_k?: number;
  max_chars?: number;
  topics?: string[];
  source_version?: string | null;
}

export interface KnowledgeQueryResult {
  items: KnowledgeReference[];
  notice: string;
}
