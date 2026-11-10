import React, { useEffect, useMemo, useRef, useState } from 'react';

export function venuePhotoUrls(thumbnail?: string, photos?: string[]): string[] {
  const values = [thumbnail, ...(Array.isArray(photos) ? photos : [])];
  return [...new Set(values.flatMap(value => {
    if (typeof value !== 'string') return [];
    const url = value.trim();
    try { return ['http:', 'https:'].includes(new URL(url).protocol) ? [url] : []; }
    catch { return []; }
  }))];
}

function Photo({ url, name, index }: { url: string; name: string; index: number }) {
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState<'loading' | 'loaded' | 'error'>('loading');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const active = useRef(false);
  useEffect(() => {
    active.current = true;
    timer.current = setTimeout(() => { active.current = false; setStatus('error'); }, 30_000);
    return () => { active.current = false; if (timer.current) clearTimeout(timer.current); };
  }, [url, attempt]);
  const finish = (next: 'loaded' | 'error') => {
    if (!active.current) return;
    active.current = false;
    if (timer.current) clearTimeout(timer.current);
    setStatus(next);
  };
  return <div className="fp-photo">
    {status !== 'error' && <img key={attempt} src={url} alt={`${name} 사진 ${index + 1}`} onLoad={() => finish('loaded')} onError={() => finish('error')} />}
    {status === 'loading' && <span className="fp-photo-loading" role="status"><span className="fp-photo-spinner" aria-hidden="true" />사진을 불러오는 중</span>}
    {status === 'error' && <div className="fp-photo-error"><span>사진을 불러오지 못했어요</span><button type="button" onClick={event => { event.stopPropagation(); setStatus('loading'); setAttempt(n => n + 1); }}>재시도</button></div>}
  </div>;
}

export function VenuePhotoGallery({ name, thumbnail, photos }: { name: string; thumbnail?: string; photos?: string[] }) {
  const urls = useMemo(() => venuePhotoUrls(thumbnail, photos), [thumbnail, photos]);
  const track = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(false);
  useEffect(() => {
    const element = track.current;
    if (!element) return;
    const measure = () => setOverflow(element.scrollWidth > element.clientWidth + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [urls.length]);
  if (!urls.length) return null;
  const move = (direction: number) => track.current?.scrollBy({ left: direction * 254, behavior: 'smooth' });
  return <div className="fp-gallery" onClick={event => event.stopPropagation()}>
    <div className="fp-photo-track" ref={track} role="region" aria-label={`${name} 사진 목록`} tabIndex={0}>
      {urls.map((url, index) => <Photo key={url} url={url} name={name} index={index} />)}
    </div>
    {overflow && <><button className="fp-photo-prev" type="button" aria-label="이전 사진" onClick={() => move(-1)}>‹</button><button className="fp-photo-next" type="button" aria-label="다음 사진" onClick={() => move(1)}>›</button></>}
  </div>;
}
