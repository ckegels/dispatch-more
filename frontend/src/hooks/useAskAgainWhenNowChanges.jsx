import { useEffect, useRef } from 'react';

// What is on a guide is said as it was when the list was asked for, and a programme ends
// while somebody is working down the list -- so the list went on showing one that had
// finished. The server says when each guide's "Now" next changes (the end of what is on,
// or the start of the next one on a guide with nothing on), and this asks again then:
// once per change rather than every few seconds in case there was one. A moment late on
// purpose, so the server is past the change when it is asked.
const LATE_BY_MS = 2000;
// setTimeout cannot wait for weeks; a change further off than this is waited for in steps,
// each asking gives the next one
const LONGEST_WAIT_MS = 6 * 3600 * 1000;

const useAskAgainWhenNowChanges = (times, ask) => {
  const asking = useRef(ask);
  useEffect(() => {
    asking.current = ask;
  }, [ask]);
  const key = times.filter(Boolean).join('|');
  useEffect(() => {
    const moment = Date.now();
    let soonest = null;
    for (const one of key ? key.split('|') : []) {
      const at = Date.parse(one);
      if (at > moment && (soonest === null || at < soonest)) soonest = at;
    }
    if (soonest === null) return undefined;
    const timer = setTimeout(
      () => asking.current(),
      Math.min(soonest - moment + LATE_BY_MS, LONGEST_WAIT_MS)
    );
    return () => clearTimeout(timer);
  }, [key]);
};

export default useAskAgainWhenNowChanges;
