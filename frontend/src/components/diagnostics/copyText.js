// One line of plain text per row, so a start or a switch can be pasted somewhere useful
// (a message, an issue) without a screenshot.

const shortTime = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });

export const startAsText = (start) => {
  const parts = [
    shortTime(start.time),
    start.channel,
    start.client,
    `took ${start.total.toFixed(1)}s`,
    start.phases.map((p) => `${p.label} ${p.at.toFixed(1)}s`).join(', '),
    `slowest: ${start.slowest}`,
  ];
  if (start.server_phases?.length) {
    parts.push(
      `media server: ${start.server_phases
        .map((p) => `${p.label} ${p.at.toFixed(1)}s`)
        .join(', ')}`
    );
    parts.push(
      start.server_gave_up
        ? 'never played'
        : `${start.server_decision}${start.server_speed ? ` ${start.server_speed}x` : ''}`
    );
  }
  return parts.filter(Boolean).join(' | ');
};

export const switchAsText = (event) => {
  const channel = event.from_channel
    ? `${event.from_channel} -> ${event.channel}`
    : event.channel || '-';
  return [
    shortTime(event.time),
    event.viewer,
    channel,
    event.action,
    event.result,
  ]
    .filter(Boolean)
    .join(' | ');
};

export const allAsText = (activity, tab) =>
  tab === 'starts'
    ? activity.starts.map(startAsText).join('\n')
    : activity.events.map(switchAsText).join('\n');

// Clipboard access can be missing or refused (no https, a locked-down browser), so the
// caller is told whether it worked instead of the click doing nothing at all.
export const copy = async (text) => {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
};
