// The version as the page shows it: v0.31.0, with the build's timestamp when there is one,
// and on a modified build "· patched" -- the short form, for the sidebar -- or the release
// it is, for About: never taken for an official release either way.
export const versionLabel = (appVersion, { full = false } = {}) => {
  const base = `v${appVersion?.version || '0.0.0'}${appVersion?.timestamp ? `-${appVersion.timestamp}` : ''}`;
  if (!appVersion?.build) return base;
  return full ? `${base} · patched (${appVersion.build})` : `${base} · patched`;
};

export default versionLabel;
