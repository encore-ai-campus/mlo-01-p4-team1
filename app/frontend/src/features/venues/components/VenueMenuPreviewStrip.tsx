import React, { useEffect, useRef, useState } from 'react';
import { MenuItem } from '../types/venue.types';

interface VenueMenuPreviewStripProps { menus: MenuItem[]; onSelectMenu?: (menu: MenuItem) => void; }
export const VenueMenuPreviewStrip: React.FC<VenueMenuPreviewStripProps> = ({ menus, onSelectMenu }) => {
  const stripRef = useRef<HTMLDivElement>(null);
  const [canGoBack, setCanGoBack] = useState(false);
  const [canGoForward, setCanGoForward] = useState(false);
  const updateControls = () => {
    const strip = stripRef.current;
    if (!strip) return;
    setCanGoBack(strip.scrollLeft > 1);
    setCanGoForward(strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 1);
  };
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    updateControls();
    const observer = new ResizeObserver(updateControls);
    observer.observe(strip);
    return () => observer.disconnect();
  }, [menus]);
  if (!menus?.length) return null;
  const slide = (direction: number) => {
    const strip = stripRef.current;
    if (!strip) return;
    const card = strip.querySelector<HTMLButtonElement>('button');
    strip.scrollBy({ left: direction * ((card?.offsetWidth ?? strip.clientWidth) + 12), behavior: 'smooth' });
  };
  return <div className="fp-menu-preview">
    <div className="fp-menu-preview-heading"><strong>대표 메뉴 미리보기</strong><div className="fp-menu-slide-controls">
      <button type="button" aria-label="이전 메뉴" disabled={!canGoBack} onClick={event => { event.stopPropagation(); slide(-1); }}>‹</button>
      <button type="button" aria-label="다음 메뉴" disabled={!canGoForward} onClick={event => { event.stopPropagation(); slide(1); }}>›</button>
    </div></div>
    <div className="fp-menu-grid" ref={stripRef} onScroll={updateControls}>{menus.map(menu => <button key={menu.menu_id} type="button" onClick={event => { event.stopPropagation(); onSelectMenu?.(menu); }}><span>{menu.menu_name}</span><strong>{menu.price > 0 ? `${menu.price.toLocaleString()}원` : '가격 미확인'}</strong></button>)}</div>
  </div>;
};
