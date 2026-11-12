import {describe, expect, it} from 'vitest';
import {guideFocusBounds} from './Guide';

const weather = {left: 16, top: 100, width: 360, height: 240};
const fullWeatherFocus = {left: 12, top: 96, width: 368, height: 248};

describe('guide focus bounds', () => {
  it('keeps the full weather panel when the mobile widget is hidden', () => {
    expect(guideFocusBounds(weather, {left: 0, top: 0, width: 0, height: 0})).toEqual(fullWeatherFocus);
  });

  it('keeps the full weather panel when the widget is positioned below it', () => {
    expect(guideFocusBounds(weather, {left: 250, top: 350, width: 120, height: 80})).toEqual(fullWeatherFocus);
  });

  it('excludes a visible widget overlapping the right side of the weather panel', () => {
    expect(guideFocusBounds(weather, {left: 250, top: 120, width: 120, height: 80}))
      .toEqual({...fullWeatherFocus, width: 230});
  });

  it('clears focus when the current guide target is absent or hidden', () => {
    expect(guideFocusBounds(null)).toBeNull();
    expect(guideFocusBounds({...weather, width: 0, height: 0})).toBeNull();
  });
});
