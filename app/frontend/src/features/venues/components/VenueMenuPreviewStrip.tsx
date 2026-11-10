import React from 'react';
import { MenuItem } from '../types/venue.types';
interface VenueMenuPreviewStripProps { menus:MenuItem[]; onSelectMenu?:(menu:MenuItem)=>void; }
export const VenueMenuPreviewStrip: React.FC<VenueMenuPreviewStripProps> = ({menus,onSelectMenu}) => {
  if (!menus?.length) return null;
  return <div className="fp-menu-preview"><strong>대표 메뉴 미리보기</strong><div className="fp-menu-grid">{menus.map(menu => <button key={menu.menu_id} type="button" onClick={e=>{e.stopPropagation();onSelectMenu?.(menu);}}><span>{menu.menu_name}</span><strong>{menu.price.toLocaleString()}원</strong></button>)}</div></div>;
};
