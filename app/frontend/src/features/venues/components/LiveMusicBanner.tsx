import React from 'react';
import { MusicBanner } from '../types/venue.types';

interface LiveMusicBannerProps {
  banner: MusicBanner | null | undefined;
}

export const LiveMusicBanner: React.FC<LiveMusicBannerProps> = ({ banner }) => {
  if (!banner || !banner.has_music_banner) {
    return null;
  }

  return (
    <div style={{
      background: '#fff0ef',
      color: '#242424',
      borderRadius: '14px',
      padding: '16px 18px',
      marginBottom: '20px',
      border: '1.5px solid #c4b5fd',
      boxShadow: '0 4px 12px rgba(36, 36, 36, 0.08)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{
            backgroundColor: '#ff7775',
            color: '#fff0ef',
            fontWeight: 800,
            fontSize: '12px',
            padding: '3px 10px',
            borderRadius: '9999px',
          }}>
            {banner.badge_text}
          </span>
        </div>
        <span style={{
          backgroundColor: '#ffffff',
          color: '#242424',
          border: '1px solid #ddd6fe',
          fontSize: '11px',
          fontWeight: 800,
          padding: '3px 9px',
          borderRadius: '6px',
        }}>
          🎧 {banner.music_mood} MOOD
        </span>
      </div>

      <div style={{ fontWeight: 900, fontSize: '16px', color: '#242424', marginBottom: '4px' }}>
        {banner.playlist_title}
      </div>

      <div style={{ fontSize: '13px', color: '#77736f', fontWeight: 500, lineHeight: 1.5 }}>
        {banner.curator_note}
      </div>

      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        marginTop: '10px',
        padding: '7px 12px',
        backgroundColor: '#ffffff',
        border: '1px solid #ddd6fe',
        borderRadius: '8px',
        fontSize: '12px',
        fontWeight: 700,
        color: '#242424',
      }}>
        <span>💿</span>
        <span>방문객을 위해 매장 분위기와 오늘 날씨에 맞춰 큐레이션된 추천 BGM 스타일입니다.</span>
      </div>
    </div>
  );
};
