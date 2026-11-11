/** Presentation state only. Nothing here grants account or administrator access. */
export const INTRO_SEEN_KEY = 'mlo:seasonal-intro:seen:v1';
export const INTRO_MESSAGE_VERSION = 1;
export const INTRO_ENTER = 'mlo:cinematic:enter';
export const INTRO_READY = 'mlo:cinematic:ready';

type StoragePort = Pick<Storage, 'getItem' | 'setItem'>;
export function hasSeenIntro(storage: StoragePort | null): boolean {
  try { return storage?.getItem(INTRO_SEEN_KEY) === '1'; } catch { return false; }
}
export function rememberIntro(storage: StoragePort | null): boolean {
  try { if (!storage) return false; storage.setItem(INTRO_SEEN_KEY, '1'); return true; } catch { return false; }
}

/** The public root is the shared platform; service and account links stay direct. */
export function rootPlatformDestination(pathname: string, search: string, hash: string): string | null {
  return pathname === '/' ? '/platform' + search + hash : null;
}

export function shouldShowIntro(pathname: string, search: string, seen: boolean): boolean {
  const landing = pathname === '/platform' || pathname === '/platform/';
  const explicitIntro = pathname === '/platform/intro' || pathname === '/platform/intro/';
  if (!landing && !explicitIntro) return false;
  const mode = new URLSearchParams(search).get('cinematic');
  if (mode === 'skip') return false;
  if (explicitIntro || mode === 'replay') return true;
  return !seen;
}

/** Return only to our own platform, preserve anchors, never trust a supplied redirect URL. */
export function platformDestination(search: string, hash: string, skip = true): string {
  const query = new URLSearchParams(search);
  query.delete('cinematic');
  if (skip) query.set('cinematic', 'skip');
  const suffix = query.toString();
  return '/platform' + (suffix ? '?' + suffix : '') + (hash.startsWith('#') ? hash : '');
}
export function isIntroMessage(data: unknown): data is { type: string; version: number } {
  if (typeof data !== 'object' || data === null) return false;
  const value = data as Record<string, unknown>;
  return value.version === INTRO_MESSAGE_VERSION && (value.type === INTRO_ENTER || value.type === INTRO_READY);
}
export function escapeAttribute(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
