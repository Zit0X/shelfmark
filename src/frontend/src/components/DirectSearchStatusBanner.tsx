import type { DirectSearchProviderStatus } from '../types';
import { Tooltip } from './shared/Tooltip';

interface StatusStyle {
  bg: string;
  text: string;
  label: string;
}

const STATUS_STYLES: Record<DirectSearchProviderStatus['status'], StatusStyle> = {
  ok: { bg: 'bg-emerald-500/20', text: 'text-emerald-700 dark:text-emerald-300', label: 'OK' },
  no_results: {
    bg: 'bg-gray-500/20',
    text: 'text-gray-700 dark:text-gray-300',
    label: 'No results',
  },
  error: { bg: 'bg-red-500/20', text: 'text-red-700 dark:text-red-300', label: 'Unavailable' },
  disabled: {
    bg: 'bg-gray-500/10',
    text: 'text-gray-500 dark:text-gray-500',
    label: 'Disabled',
  },
  not_configured: {
    bg: 'bg-amber-500/20',
    text: 'text-amber-700 dark:text-amber-300',
    label: 'Not configured',
  },
};

interface DirectSearchStatusBannerProps {
  providerStatuses: DirectSearchProviderStatus[];
}

/**
 * Per-provider outcome for the Direct-mode metadata fallback (see useSearch /
 * searchDirectEnriched): "Open Library: no results", "Google Books: unavailable
 * (timed out)", etc., instead of the search silently returning nothing extra.
 */
export const DirectSearchStatusBanner = ({ providerStatuses }: DirectSearchStatusBannerProps) => {
  if (providerStatuses.length === 0) {
    return null;
  }

  return (
    <div
      className="animate-pop-up mb-3 flex flex-wrap items-center gap-1.5"
      role="status"
      aria-label="Search provider status"
    >
      {providerStatuses.map((status) => {
        const style = STATUS_STYLES[status.status];
        const summary =
          status.status === 'ok'
            ? `${status.display_name}: ${status.count} result${status.count === 1 ? '' : 's'}`
            : `${status.display_name}: ${style.label}`;

        return (
          <Tooltip key={status.name} content={status.message || null}>
            <span
              className={`${style.bg} ${style.text} inline-flex items-center rounded-full px-2.5 py-1 text-xs font-medium whitespace-nowrap`}
            >
              {summary}
            </span>
          </Tooltip>
        );
      })}
    </div>
  );
};
