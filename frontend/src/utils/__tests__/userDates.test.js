import { afterEach, describe, expect, it } from 'vitest';
import {
  userDateTime,
  userIsToday,
  userTime,
  userWeekdayTime,
} from '../userDates';

// 2026-11-22 17:04:07 UTC, a Sunday
const AT = Date.UTC(2026, 10, 22, 17, 4, 7);

const choose = (dateFormat, timeFormat, zone = 'UTC') => {
  localStorage.setItem('date-format', JSON.stringify(dateFormat));
  localStorage.setItem('time-format', JSON.stringify(timeFormat));
  localStorage.setItem('time-zone', JSON.stringify(zone));
};

describe('userDates: the fork pages show times as Settings -> UI says', () => {
  afterEach(() => localStorage.clear());

  it('ISO 8601 and the 24-hour clock (Dispatcharr 0.34)', () => {
    choose('ymd', '24h');
    expect(userDateTime(AT)).toBe('2026-11-22, 17:04:07');
    expect(userTime(AT)).toBe('17:04');
    expect(userTime(AT, { seconds: true })).toBe('17:04:07');
  });

  it('day first, and the 12-hour clock as stock writes it', () => {
    choose('dmy', '12h');
    expect(userDateTime(AT)).toBe('22/11/2026, 5:04:07 PM');
    expect(userTime(AT)).toBe('5:04pm');
  });

  it('in the chosen time zone, not the browser’s', () => {
    choose('ymd', '24h', 'Europe/Vienna');
    expect(userTime(AT)).toBe('18:04');
    expect(userWeekdayTime(AT, { dayOfMonth: true })).toBe('Sun 22 18:04');
  });

  it('nothing chosen: the stock defaults (month first, 12-hour)', () => {
    localStorage.setItem('time-zone', JSON.stringify('UTC'));
    expect(userDateTime(AT)).toBe('11/22/2026, 5:04:07 PM');
  });

  it('nothing to show is an empty string, never "Invalid Date"', () => {
    choose('ymd', '24h');
    expect(userTime(null)).toBe('');
    expect(userDateTime('')).toBe('');
    expect(userDateTime('not a date')).toBe('');
    expect(userWeekdayTime(undefined)).toBe('');
  });

  it('today is today in the chosen zone', () => {
    choose('ymd', '24h');
    expect(userIsToday(Date.now())).toBe(true);
    expect(userIsToday(Date.now() - 3 * 86400 * 1000)).toBe(false);
  });
});
