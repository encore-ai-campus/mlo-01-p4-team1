export const NICKNAME_HELP='한글·영문·숫자 2~10자 · 공백 불가';
export const PASSWORD_HELP='영문·숫자·특수문자 8~20자 · 대문자 1개 이상 · 특수문자 2개 이상 · 공백 불가';

export function nicknameValid(value:string){
  return /^[A-Za-z0-9\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]{2,10}(?![\s\S])/u.test(value.normalize('NFKC'));
}

export function passwordChecks(value:string){
  return {
    length:value.length>=8&&value.length<=20,
    characters:value.length>0&&!/[^!-~]/.test(value),
    uppercase:/[A-Z]/.test(value),
    specials:(value.match(/[^A-Za-z0-9]/g)||[]).length>=2,
  };
}

export const passwordValid=(value:string)=>Object.values(passwordChecks(value)).every(Boolean);
