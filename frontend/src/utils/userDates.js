// Dates and times on Dispatch More's own pages, shown the way the rest of Dispatcharr shows
// them: in the time zone, the date format (MM/DD/YYYY, DD/MM/YYYY or, since Dispatcharr 0.34,
// ISO 8601) and the 12- or 24-hour clock chosen in Settings -> UI. They used the browser's own
// locale, so a page of the fork could say "11/22/2026, 6:04:00 PM" next to a stock page saying
// "2026-11-22, 18:04:00".
//
// Plain functions rather than a hook, because most of them are formatters outside a component:
// they read the same browser storage the settings form writes (useBrowserStorage), on every
// call, so a change of setting shows on the next render. Nothing chosen: the stock defaults.
import dayjs from 'dayjs';
import { getDateFormats } from './dateTimeUtils';
import { readStoredJSON } from '../hooks/useBrowserStorage';

const choices = () => ({
  twelveHour: readStoredJSON('time-format', '12h') === '12h',
  dates: getDateFormats(readStoredJSON('date-format', 'mdy')),
  zone: readStoredJSON('time-zone', null),
});

// value: anything dayjs reads (a Date, an ISO string, milliseconds). Invalid or empty: null.
const inZone = (value, zone) => {
  if (value === null || value === undefined || value === '') return null;
  const moment = dayjs(value);
  if (!moment.isValid()) return null;
  if (!zone) return moment;
  try {
    return moment.tz(zone);
  } catch {
    return moment;
  }
};

// The same clock formats as stock's useDateTimeFormat (timeFormat, fullTimeFormat)
const clock = (twelveHour, seconds) =>
  twelveHour
    ? seconds
      ? 'h:mm:ss A'
      : 'h:mma'
    : seconds
      ? 'HH:mm:ss'
      : 'HH:mm';

/** "18:04" or "6:04pm" (with seconds: "18:04:07" or "6:04:07 PM"); '' for nothing. */
export const userTime = (value, { seconds = false } = {}) => {
  const { twelveHour, zone } = choices();
  const moment = inZone(value, zone);
  return moment ? moment.format(clock(twelveHour, seconds)) : '';
};

/** Date and time as stock's fullDateTimeFormat: "2026-11-22, 18:04:07"; '' for nothing. */
export const userDateTime = (value) => {
  const { twelveHour, dates, zone } = choices();
  const moment = inZone(value, zone);
  return moment
    ? moment.format(`${dates.fullDateFormat}, ${clock(twelveHour, true)}`)
    : '';
};

/** A weekday and the time, "Sun 18:04" (dayOfMonth: "Sun 22 18:04"); '' for nothing. */
export const userWeekdayTime = (value, { dayOfMonth = false } = {}) => {
  const { twelveHour, zone } = choices();
  const moment = inZone(value, zone);
  return moment
    ? moment.format(`ddd${dayOfMonth ? ' D' : ''} ${clock(twelveHour, false)}`)
    : '';
};

/** True when value falls on today, in the chosen time zone. */
export const userIsToday = (value) => {
  const { zone } = choices();
  const moment = inZone(value, zone);
  const now = inZone(Date.now(), zone);
  return Boolean(moment && now && moment.format('YYYY-MM-DD') === now.format('YYYY-MM-DD'));
};
