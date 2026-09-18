// The version as the page shows it: v0.31.0, with the build's timestamp when there is one,
// and "+mod" on a modified build so it is never taken for an official release.
export const versionLabel = (appVersion) =>
  `v${appVersion?.version || '0.0.0'}${appVersion?.timestamp ? `-${appVersion.timestamp}` : ''}${
    appVersion?.build ? `+${appVersion.build}` : ''
  }`;

export default versionLabel;
