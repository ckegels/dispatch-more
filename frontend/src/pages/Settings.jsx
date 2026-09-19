import React, { Suspense } from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
  Alert,
  Anchor,
  Box,
  Divider,
  Loader,
  Paper,
  Text,
} from '@mantine/core';
import { getVisibleSettingsGroups } from '../config/settingsNav';
import useAuthStore from '../store/auth';
import useSettingsStore from '../store/settings';
import { USER_LEVELS } from '../constants';
import ErrorBoundary from '../components/ErrorBoundary.jsx';

const SettingsPage = () => {
  const authUser = useAuthStore((s) => s.user);
  const location = useLocation();
  const appVersion = useSettingsStore((s) => s.version);
  const isAdmin = authUser.user_level >= USER_LEVELS.ADMIN;

  const activeSection = location.hash.replace('#', '') || null;

  const visibleGroups = getVisibleSettingsGroups(isAdmin);
  const allSections = visibleGroups.flatMap((g) => g.sections);
  const activeSectionConfig = activeSection
    ? (allSections.find((s) => s.id === activeSection) ?? null)
    : null;
  const ActiveComponent = activeSectionConfig?.Component ?? null;

  // Most settings read better narrow; a page with a wide table asks for the room
  const maxWidth = activeSectionConfig?.wide ? 1600 : 900;

  return (
    <Box p={10} maw={maxWidth} mx="auto">
      {appVersion?.build && activeSection !== 'modified-build' && (
        // A modified build says so wherever its settings are changed
        <Alert color="orange" variant="light" mb="sm" p="xs">
          <Text size="xs">
            This is {appVersion.build}, a modified build of Dispatcharr{' '}
            {appVersion.version} — not official Dispatcharr. Before reporting a
            problem to Dispatcharr, uninstall it and try stock.{' '}
            <Anchor component={Link} to="/settings#modified-build" size="xs">
              Modified build
            </Anchor>
          </Text>
        </Alert>
      )}
      {ActiveComponent ? (
        <Paper withBorder p="md" radius="md">
          <Text size="lg" fw={600} mb={6}>
            {activeSectionConfig.label}
          </Text>
          <Divider mb="md" />
          <ErrorBoundary inline>
            <Suspense fallback={<Loader />}>
              <ActiveComponent active={true} />
            </Suspense>
          </ErrorBoundary>
        </Paper>
      ) : (
        <Box
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            minHeight: 200,
          }}
        >
          <Text c="dimmed" size="sm">
            Select a setting from the sidebar
          </Text>
        </Box>
      )}
    </Box>
  );
};

export default SettingsPage;
