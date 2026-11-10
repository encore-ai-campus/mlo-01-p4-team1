import { afterEach, describe, expect, it, vi } from 'vitest';
import { withMinimumRecommendationDelay } from './RecommendationLoading';

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
describe('추천 최소 로딩 시간', () => {
  it('빠른 응답은 최소 시간이 끝날 때 표시한다', async () => {
    vi.useFakeTimers(); vi.stubGlobal('window', globalThis);
    const done = vi.fn();
    const promise = withMinimumRecommendationDelay(Promise.resolve('결과'), 500).then(done);
    await vi.advanceTimersByTimeAsync(499); expect(done).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1); await promise; expect(done).toHaveBeenCalledWith('결과');
  });
  it('최소 시간이 지나도 요청이 진행 중이면 기다린다', async () => {
    vi.useFakeTimers(); vi.stubGlobal('window', globalThis);
    let finish!: (value: string) => void;
    const request = new Promise<string>(resolve => { finish = resolve; });
    const done = vi.fn(); const promise = withMinimumRecommendationDelay(request, 700).then(done);
    await vi.advanceTimersByTimeAsync(700); expect(done).not.toHaveBeenCalled();
    finish('늦은 결과'); await promise; expect(done).toHaveBeenCalledWith('늦은 결과');
  });
  it('요청 오류도 최소 시간이 지난 뒤 전달한다', async () => {
    vi.useFakeTimers(); vi.stubGlobal('window', globalThis);
    const done = vi.fn(); const promise = withMinimumRecommendationDelay(Promise.reject(new Error('오류')), 500).catch(done);
    await vi.advanceTimersByTimeAsync(499); expect(done).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1); await promise; expect(done).toHaveBeenCalledOnce();
  });
});
