import React from 'react';
import {
  Alert,
  Box,
  Button,
  Divider,
  Group,
  Modal,
  SimpleGrid,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core';
import { BookOpen, Heart, TriangleAlert, Users } from 'lucide-react';
import { DiscordIcon, GitHubIcon } from './icons.jsx';
import logo from '../images/logo.png';
import useSettingsStore from '../store/settings';
import { versionLabel } from '../utils/versionLabel';

const AboutModal = ({ isOpen, onClose }) => {
  const appVersion = useSettingsStore((s) => s.version);
  const versionString = versionLabel(appVersion, { full: true });

  return (
    <Modal
      opened={isOpen}
      onClose={onClose}
      title="About Dispatcharr"
      centered
      size="md"
    >
      <Stack gap="lg">
        <Group justify="center" gap="md">
          <img src={logo} alt="Dispatcharr" width={56} />
          <Stack gap={2}>
            <Text fw={700} size="xl">
              Dispatcharr
            </Text>
            <Text size="sm" c="dimmed">
              {versionString}
            </Text>
          </Stack>
        </Group>

        {appVersion?.build && (
          // A modified build: said first, so a problem with it is not taken to the
          // Dispatcharr developers, who did not write it
          <Alert
            color="orange"
            variant="light"
            icon={<TriangleAlert size={18} />}
            title="A modified build, not official Dispatcharr"
          >
            <Stack gap={6}>
              <Text size="sm">
                This Dispatcharr {appVersion.version} carries changes of its
                own: Channel Switch Overlap, media servers, Channel Manager,
                Stream Check, Find Logos and more. The Dispatcharr developers
                did not write them and do not support them.
              </Text>
              <Text size="sm">
                If something goes wrong, first uninstall the changes and try the
                same thing on stock Dispatcharr:
              </Text>
              <Text size="sm">
                Settings → System → Modified build has a button for it; the
                uninstall script does the same by hand.
              </Text>
              <Text size="sm" fw={600}>
                Do not report problems with this build on the official
                Dispatcharr GitHub or Discord. Only a problem that also happens
                on stock Dispatcharr, after uninstalling, belongs there.
              </Text>
            </Stack>
          </Alert>
        )}

        <Divider />

        <SimpleGrid cols={2} spacing="sm">
          <Tooltip label="Visit the Dispatcharr documentation" position="top">
            <Button
              component="a"
              href="https://dispatcharr.github.io/Dispatcharr-Docs/"
              target="_blank"
              rel="noopener noreferrer"
              variant="default"
              leftSection={<BookOpen size={15} />}
              fullWidth
            >
              Documentation
            </Button>
          </Tooltip>
          <Tooltip label="Join our Discord community" position="top">
            <Button
              component="a"
              href="https://discord.gg/Sp45V5BcxU"
              target="_blank"
              rel="noopener noreferrer"
              variant="default"
              leftSection={<DiscordIcon size={15} />}
              fullWidth
            >
              Discord
            </Button>
          </Tooltip>
          <Tooltip label="View source on GitHub" position="top">
            <Button
              component="a"
              href="https://github.com/Dispatcharr/Dispatcharr"
              target="_blank"
              rel="noopener noreferrer"
              variant="default"
              leftSection={<GitHubIcon size={15} />}
              fullWidth
            >
              GitHub
            </Button>
          </Tooltip>
          <Tooltip
            label="Support Dispatcharr on Open Collective"
            position="top"
          >
            <Button
              component="a"
              href="https://opencollective.com/dispatcharr/contribute"
              target="_blank"
              rel="noopener noreferrer"
              variant="default"
              color="pink"
              leftSection={<Heart size={15} />}
              fullWidth
            >
              Donate
            </Button>
          </Tooltip>
        </SimpleGrid>

        <Divider />

        <Stack gap="xs">
          <Group gap="xs">
            <Users size={16} />
            <Text size="sm" fw={500}>
              Contributors
            </Text>
          </Group>
          <Text size="sm" c="dimmed">
            Dispatcharr is built by the community, for the community. Thank you
            to every contributor, tester, and supporter who has helped make this
            project what it is.
          </Text>
        </Stack>

        <Tooltip label="Remembering Jesse Mann" position="top" withArrow>
          <Box
            style={{
              background: 'var(--mantine-color-dark-6)',
              borderRadius: 'var(--mantine-radius-sm)',
              borderLeft: '3px solid var(--mantine-color-pink-5)',
              padding: '10px 14px',
              cursor: 'default',
            }}
          >
            <Text size="sm" c="dimmed">
              In memory of{' '}
              <Text span fw={600} c="gray.3">
                Jesse Mann
              </Text>
              .
            </Text>
          </Box>
        </Tooltip>
      </Stack>
    </Modal>
  );
};

export default AboutModal;
