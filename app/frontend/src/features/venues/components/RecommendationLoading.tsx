export async function withMinimumRecommendationDelay<T>(request: Promise<T>, delayMs = 300 + Math.floor(Math.random() * 301)): Promise<T> {
  const minimum = new Promise<void>(resolve => window.setTimeout(resolve, delayMs));
  try { return await request; } finally { await minimum; }
}

export function RecommendationLoading() {
  return <div className="fp-ai-loading" role="status" aria-live="polite">
    <span className="fp-ai-spinner" aria-hidden="true" />
    <span>조건에 맞는 매장을 찾고 있어요.</span>
  </div>;
}
