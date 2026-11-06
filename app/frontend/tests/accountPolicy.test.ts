import {describe,it,expect} from 'vitest';
import {nicknameValid,passwordValid} from '../src/shared/accountPolicy';

describe('approved account input policy',()=>{
  it.each(['가나','한글Ab12','Ab','12','회원0007','Abcde12345','ＡＢ','ㄱㄴ'])('accepts nickname %s',value=>expect(nicknameValid(value)).toBe(true));
  it.each(['가','Abcde123456','한 글',' ab','ab ','ab\n','ab\t','漢字','😀😀','ab!'])('rejects nickname %s',value=>expect(nicknameValid(value)).toBe(false));
  it.each(['Abcd12!!','A'+'b'.repeat(17)+'!!','ABCDE!!F','Abcd12!@'])('accepts password boundaries and punctuation occurrences %s',value=>expect(passwordValid(value)).toBe(true));
  it.each(['Abcd1!!','A'+'b'.repeat(18)+'!!','abcd12!!','Abcd123!','Abc d1!!','Abcd12!!\n','Abcd12!!\t','Abcd한글!!','Ａbcd12!!'])('rejects password %s',value=>expect(passwordValid(value)).toBe(false));
});
