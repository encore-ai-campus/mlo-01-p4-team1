import type {ReactNode} from 'react';

export function Notice({children, error = false}: {children?: ReactNode; error?: boolean}) {
  if (!children) return null;
  return <p className={`p4-notice${error ? ' p4-notice-error' : ''}`} role={error ? 'alert' : 'status'}>{children}</p>;
}
export function ResourceState({loading, error, retry}: {loading: boolean; error: string; retry: () => void}) {
  return <>{loading && <Notice>불러오는 중입니다…</Notice>}{error && <><Notice error>{error}</Notice><button type="button" onClick={retry}>다시 불러오기</button></>}</>;
}
export function ActionState({pending, error, message}: {pending: boolean; error: string; message: string}) {
  return <>{pending && <Notice>요청을 처리하고 있습니다…</Notice>}<Notice error>{error}</Notice><Notice>{message}</Notice></>;
}
export function Empty({children}: {children: ReactNode}) {return <p className="p4-empty">{children}</p>;}

export function ListPagination({label, page, hasMore, loading, onPage}: {
  label: string;
  page: number;
  hasMore?: boolean;
  loading: boolean;
  onPage: (page: number) => void;
}) {
  if (!page && !hasMore) return null;
  return <nav className="p4-actions" aria-label={label}>
    <span className="p4-meta" aria-live="polite">{page + 1}페이지</span>
    <button type="button" className="p4-secondary" disabled={loading || !page} onClick={() => onPage(page - 1)}>이전</button>
    <button type="button" className="p4-secondary" disabled={loading || !hasMore} onClick={() => onPage(page + 1)}>다음</button>
  </nav>;
}
