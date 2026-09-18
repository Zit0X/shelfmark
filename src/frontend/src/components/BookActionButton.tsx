import type { CSSProperties } from 'react';

import { useSearchMode } from '../contexts/SearchModeContext';
import type { Book, ButtonStateInfo } from '../types';
import { isDiscoveryOnlyBook } from '../types';
import { BookDownloadButton } from './BookDownloadButton';
import { BookGetButton } from './BookGetButton';

type ButtonSize = 'sm' | 'md';
type ButtonVariant = 'default' | 'icon';

interface BookActionButtonProps {
  book: Book;
  buttonState: ButtonStateInfo;
  onDownload: (book: Book) => Promise<void>;
  onGetReleases: (book: Book) => void;
  isLoadingReleases?: boolean;
  size?: ButtonSize;
  variant?: ButtonVariant;
  fullWidth?: boolean;
  className?: string;
  style?: CSSProperties;
}

export function BookActionButton({
  book,
  buttonState,
  onDownload,
  onGetReleases,
  isLoadingReleases,
  size,
  variant = 'default',
  fullWidth,
  className,
  style,
}: BookActionButtonProps) {
  const { searchMode } = useSearchMode();

  // Universal mode always searches release sources first; Direct mode does too for a
  // discovery-only result (found by a metadata provider, no release of its own yet -
  // see the Direct-mode fallback in useSearch/searchDirectEnriched). A Direct-mode
  // result with its own release (Anna's Archive, ...) downloads immediately either way.
  if (searchMode === 'universal' || isDiscoveryOnlyBook(book)) {
    return (
      <BookGetButton
        book={book}
        onGetReleases={onGetReleases}
        buttonState={buttonState}
        isLoading={isLoadingReleases}
        size={size}
        variant={variant}
        fullWidth={fullWidth}
        className={className}
        style={style}
      />
    );
  }

  return (
    <BookDownloadButton
      buttonState={buttonState}
      onDownload={() => onDownload(book)}
      size={size}
      variant={variant === 'default' ? 'primary' : 'icon'}
      fullWidth={fullWidth}
      className={className}
      style={style}
      ariaLabel={buttonState.text}
    />
  );
}
