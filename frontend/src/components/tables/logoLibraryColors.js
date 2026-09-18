// What the channel already has looks different from what a collection guessed for it: it is
// bound to the channel by its mapping, not found by its name
export const sourceColor = (source) =>
  ({
    'your guide': 'violet',
    'your playlist': 'violet',
    'tv-logos': 'teal',
    'iptv-org': 'blue',
  })[source] || 'cyan';
