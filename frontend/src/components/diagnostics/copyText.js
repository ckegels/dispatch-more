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
    if (start.server_title) {
      parts.push(`matched to "${start.server_title}"`);
    }
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

// navigator.clipboard only exists in a secure context, and Dispatcharr is usually opened over
// plain http on a local address, so the old way of copying is the one that actually works
// here. The caller is told whether it worked instead of the click doing nothing at all.
const copyWithTextarea = (text) => {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  // Off-screen, and not focusable by tab, so nothing jumps while copying
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.top = '-1000px';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  try {
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    document.body.removeChild(textarea);
  }
};

export const copy = async (text) => {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Refused (not a secure context, or no permission): fall through and try the old way
  }
  return copyWithTextarea(text);
};
