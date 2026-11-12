import {useCallback, useEffect, useRef, useState} from 'react';
import {Phase4Error} from './transport';

export const errorText = (error: unknown) => error instanceof Phase4Error || error instanceof Error ? error.message : '처리를 완료하지 못했습니다. 다시 시도해 주세요.';

export function useResource<T>(loader: (signal: AbortSignal) => Promise<T>, dependencies: readonly unknown[]) {
  const [value, setValue] = useState<T | null>(null), [loading, setLoading] = useState(true), [error, setError] = useState(''), [revision, setRevision] = useState(0);
  const sequence = useRef(0);
  useEffect(() => {
    const request = new AbortController(), current = ++sequence.current;
    setLoading(true); setError(''); setValue(null);
    void loader(request.signal).then(next => {if (!request.signal.aborted && current === sequence.current) setValue(next);})
      .catch(reason => {if (!request.signal.aborted && current === sequence.current) setError(errorText(reason));})
      .finally(() => {if (!request.signal.aborted && current === sequence.current) setLoading(false);});
    return () => {request.abort();};
    // Loader dependencies are explicitly supplied by each resource owner.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, revision]);
  return {value, loading, error, reload: useCallback(() => setRevision(value => value + 1), [])};
}

export function useAction() {
  const [pending, setPending] = useState(false), [error, setError] = useState(''), [message, setMessage] = useState('');
  const busy = useRef(false), mounted = useRef(true);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  async function run<T>(operation: () => Promise<T>, completed: (value: T) => void, success: string) {
    if (busy.current) return;
    busy.current = true; setPending(true); setError(''); setMessage('');
    try {const result = await operation(); if (mounted.current) {completed(result); setMessage(success);}}
    catch (reason) {if (mounted.current) setError(errorText(reason));}
    finally {busy.current = false; if (mounted.current) setPending(false);}
  }
  return {pending, error, message, run};
}
