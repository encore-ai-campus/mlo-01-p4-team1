import {describe, expect, it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {BusinessNotice} from '../src/features/platform/BusinessNotice';

describe('매장 운영자 가입 안내', () => {
  it('공통 가입과 매장 연결을 안내하고 확인되지 않는 절차를 표시하지 않는다', () => {
    const html = renderToStaticMarkup(<BusinessNotice open onClose={() => {}}/>);
    expect(html).toContain('공통 회원가입');
    expect(html).toContain('매장 연결');
    expect(html).toContain('뮤더에서 선곡');
    expect(html).toContain('실제 매장 소유 여부를 확인하지 않습니다');
    expect(html).not.toContain('국세청');
    expect(html).not.toContain('심사');
  });
});
