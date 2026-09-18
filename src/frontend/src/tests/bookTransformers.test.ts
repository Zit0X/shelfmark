import { describe, it, expect } from 'vitest';

import {
  getDiscoverySources,
  isDiscoveryOnlyBook,
  isMetadataBook,
  type Release,
} from '../types/index';
import {
  transformDiscoveryToBook,
  transformReleaseToDirectBook,
  transformSourceRecordToBook,
  type DiscoveryBookData,
} from '../utils/bookTransformers';

describe('bookTransformers.transformReleaseToDirectBook', () => {
  it('maps direct release data into the direct-mode book card shape', () => {
    const release: Release = {
      source: 'direct_download',
      source_id: 'md5-123',
      title: 'Example Title',
      format: 'epub',
      language: 'en',
      size: '2 MB',
      info_url: 'https://example.com/md5/md5-123',
      extra: {
        author: 'Example Author',
        year: '2001',
        preview: 'https://example.com/cover.jpg',
        publisher: 'Example Publisher',
        description: 'Example description',
        info: {
          Downloads: ['42'],
        },
      },
    };

    const book = transformReleaseToDirectBook(release);

    expect(book.id).toBe('md5-123');
    expect(book.title).toBe('Example Title');
    expect(book.author).toBe('Example Author');
    expect(book.year).toBe('2001');
    expect(book.language).toBe('en');
    expect(book.format).toBe('epub');
    expect(book.size).toBe('2 MB');
    expect(book.preview).toBe('https://example.com/cover.jpg');
    expect(book.publisher).toBe('Example Publisher');
    expect(book.description).toBe('Example description');
    expect(book.source).toBe('direct_download');
    expect(book.provider).toBe('direct_download');
    expect(book.provider_id).toBe('md5-123');
    expect(book.provider_display_name).toBe('Direct Download');
    expect(book.source_url).toBe('https://example.com/md5/md5-123');
    expect(book.info).toEqual({ Downloads: ['42'] });
    expect(isMetadataBook(book)).toBe(false);
  });
});

describe('bookTransformers.transformSourceRecordToBook', () => {
  it('maps source-native records into source-backed book context', () => {
    const book = transformSourceRecordToBook({
      id: 'md5-456',
      title: 'Record Title',
      source: 'direct_download',
      author: 'Record Author',
      preview: '/api/covers/md5-456',
      year: 1999,
      language: 'en',
      format: 'epub',
      size: '3 MB',
      publisher: 'Record Publisher',
      description: 'Record description',
      info: {
        Downloads: ['64'],
      },
      source_url: 'https://example.com/record/md5-456',
    });

    expect(book.id).toBe('md5-456');
    expect(book.title).toBe('Record Title');
    expect(book.author).toBe('Record Author');
    expect(book.source).toBe('direct_download');
    expect(book.provider).toBe('direct_download');
    expect(book.provider_id).toBe('md5-456');
    expect(book.provider_display_name).toBe('Direct Download');
    expect(book.year).toBe('1999');
    expect(book.preview).toBe('/api/covers/md5-456');
    expect(book.source_url).toBe('https://example.com/record/md5-456');
    expect(book.info).toEqual({ Downloads: ['64'] });
    expect(isMetadataBook(book)).toBe(false);
  });
});

describe('bookTransformers.transformDiscoveryToBook', () => {
  const discoveryData: DiscoveryBookData = {
    provider: 'openlibrary',
    provider_id: 'OL1W',
    provider_display_name: 'Open Library',
    title: 'Meurtre au festival des citrouilles',
    authors: ['Ava Manceau'],
    language: 'fr',
    sources: [
      { provider: 'openlibrary', provider_id: 'OL1W', provider_display_name: 'Open Library' },
      { provider: 'googlebooks', provider_id: 'vol-1', provider_display_name: 'Google Books' },
    ],
    match_basis: 'title_author_lang',
    relevance_score: 0.85,
  };

  it('maps a discovery-only result into the metadata-provider book card shape', () => {
    const book = transformDiscoveryToBook(discoveryData);

    expect(book.id).toBe('openlibrary:OL1W');
    expect(book.title).toBe('Meurtre au festival des citrouilles');
    expect(book.author).toBe('Ava Manceau');
    expect(book.provider).toBe('openlibrary');
    expect(book.provider_id).toBe('OL1W');
    expect(book.source).toBeUndefined();
  });

  it('is treated as a metadata book and as discovery-only, unlike a direct release', () => {
    const book = transformDiscoveryToBook(discoveryData);

    expect(isMetadataBook(book)).toBe(true);
    expect(isDiscoveryOnlyBook(book)).toBe(true);
  });

  it('carries every source that reported the book', () => {
    const book = transformDiscoveryToBook(discoveryData);

    expect(getDiscoverySources(book)).toEqual(discoveryData.sources);
  });

  it('a plain direct-mode release is neither a metadata book nor discovery-only', () => {
    const release: Release = {
      source: 'direct_download',
      source_id: 'md5-123',
      title: 'Example Title',
    };

    const book = transformReleaseToDirectBook(release);

    expect(isMetadataBook(book)).toBe(false);
    expect(isDiscoveryOnlyBook(book)).toBe(false);
    expect(getDiscoverySources(book)).toBeUndefined();
  });
});
