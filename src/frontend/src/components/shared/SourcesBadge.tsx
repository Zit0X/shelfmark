import type { Book } from '../../types';
import { getDiscoverySources } from '../../types';
import { Tooltip } from './Tooltip';

interface SourcesBadgeProps {
  book: Book;
  className?: string;
}

/**
 * Small "found by N sources" indicator for a Direct-mode discovery result that was
 * merged from more than one metadata provider (see metadata_dedup on the backend).
 * Renders nothing for a single-source result - most books don't need this.
 */
export function SourcesBadge({ book, className = '' }: SourcesBadgeProps) {
  const sources = getDiscoverySources(book);
  if (!sources || sources.length < 2) {
    return null;
  }

  const names = sources.map((source) => source.provider_display_name || source.provider);

  return (
    <Tooltip content={`Found via ${names.join(', ')}`}>
      <span
        className={`inline-flex items-center gap-1 rounded-full bg-sky-500/20 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:text-sky-300 ${className}`}
      >
        <svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M13 10V3L4 14h7v7l9-11h-7z"
          />
        </svg>
        {sources.length} sources
      </span>
    </Tooltip>
  );
}
