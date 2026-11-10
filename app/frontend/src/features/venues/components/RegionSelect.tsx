import React, { useEffect, useId, useMemo, useRef, useState } from 'react';

export interface RegionGroup { label: string; options: string[]; }
interface RegionSelectProps { value: string; groups: RegionGroup[]; onChange: (value: string) => void; }

export function nextRegionIndex(key: string, current: number, length: number): number {
  if (!length) return -1;
  if (key === 'Home') return 0;
  if (key === 'End') return length - 1;
  if (key === 'ArrowDown') return Math.min(length - 1, current + 1);
  if (key === 'ArrowUp') return Math.max(0, current - 1);
  return current;
}

export function RegionSelect({ value, groups, onChange }: RegionSelectProps) {
  const id = useId();
  const listId = `${id}-list`;
  const options = useMemo(() => groups.flatMap(group => group.options), [groups]);
  const selectedIndex = Math.max(0, options.indexOf(value));
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(selectedIndex);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef({ text: '', at: 0 });

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [open]);
  useEffect(() => {
    if (!open || activeIndex < 0) return;
    const item = listRef.current?.querySelector<HTMLElement>(`[data-region-index="${activeIndex}"]`);
    if (!item || !listRef.current) return;
    const list = listRef.current;
    const top = item.offsetTop;
    const bottom = top + item.offsetHeight;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
  }, [open, activeIndex]);

  const show = () => { setActiveIndex(selectedIndex); setOpen(true); };
  const choose = (index: number) => {
    const next = options[index];
    if (next !== undefined) onChange(next);
    setOpen(false);
    buttonRef.current?.focus();
  };
  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'Tab') { setOpen(false); return; }
    if (event.key === 'Escape') { if (open) { event.preventDefault(); setOpen(false); } return; }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (open) choose(activeIndex); else show();
      return;
    }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        setActiveIndex(event.key === 'Home' || event.key === 'End' ? nextRegionIndex(event.key, selectedIndex, options.length) : selectedIndex);
      } else setActiveIndex(current => nextRegionIndex(event.key, current, options.length));
      return;
    }
    if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key.length === 1 && event.key.trim()) {
      const now = Date.now();
      const typed = (now - searchRef.current.at < 700 ? searchRef.current.text : '') + event.key.toLocaleLowerCase();
      searchRef.current = { text: typed, at: now };
      const index = options.findIndex(option => option.toLocaleLowerCase().startsWith(typed));
      if (index >= 0) { event.preventDefault(); setOpen(true); setActiveIndex(index); }
    }
  };
  let offset = 0;
  return <div className="fp-region-select" ref={rootRef}>
    <button ref={buttonRef} type="button" className="fp-region-trigger" role="combobox" aria-label="지역 선택" aria-expanded={open} aria-controls={listId} aria-haspopup="listbox" aria-activedescendant={open && activeIndex >= 0 ? `${id}-option-${activeIndex}` : undefined} onClick={() => open ? setOpen(false) : show()} onKeyDown={onKeyDown}>
      <span>{value}</span><span className="fp-region-chevron" aria-hidden="true">⌄</span>
    </button>
    {open && <div id={listId} className="fp-region-list" role="listbox" aria-label="지역 목록" ref={listRef}>
      {groups.map(group => {
        const start = offset; offset += group.options.length;
        return <div role="group" aria-label={group.label} key={group.label}>
          <div className="fp-region-group-label" aria-hidden="true">{group.label}</div>
          {group.options.map((option, index) => {
            const optionIndex = start + index;
            return <div key={option} id={`${id}-option-${optionIndex}`} data-region-index={optionIndex} role="option" aria-selected={option === value} className={`fp-region-option${activeIndex === optionIndex ? ' is-active' : ''}${option === value ? ' is-selected' : ''}`} onMouseDown={event => event.preventDefault()} onClick={() => choose(optionIndex)}>
              <span>{option}</span>{option === value && <span aria-hidden="true">✓</span>}
            </div>;
          })}
        </div>;
      })}
    </div>}
  </div>;
}
