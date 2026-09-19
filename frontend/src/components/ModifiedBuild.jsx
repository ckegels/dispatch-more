import React, { useEffect, useState } from 'react';
import { Alert, Button, Stack, Table, Text } from '@mantine/core';
import { TriangleAlert, Undo2 } from 'lucide-react';
import API from '../api';
import ConfirmationDialog from './ConfirmationDialog';

// Settings → System → Modified build: what this build is, and a button to put stock
// Dispatcharr back. The button only leaves a request -- the web app is not allowed to
// replace its own files or restart its services -- which the installer's watcher (or, in
// Docker, the next start of the container) carries out.

const LAYOUTS = { docker: 'Docker', systemd: 'Linux / LXC (systemd)' };

const ModifiedBuild = () => {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [asking, setAsking] = useState(false);
  const [done, setDone] = useState(null);

  const load = async () => {
    try {
      setStatus(await API.getModifiedBuild());
    } catch {
      setError('Could not read what this build is.');
    }
  };

  useEffect(() => {
    load();
  }, []);

  const uninstall = async () => {
    setAsking(false);
    setError(null);
    try {
      const answer = await API.uninstallModifiedBuild();
      setDone(answer.how);
      await load();
    } catch (e) {
      setError(e?.body?.error || 'Could not ask for it to be uninstalled.');
    }
  };

  if (!status) return error ? <Alert color="red">{error}</Alert> : null;
  const record = status.record || {};

  return (
    <Stack gap="md">
      <Alert
        color="orange"
        variant="light"
        icon={<TriangleAlert size={18} />}
        title="A modified build, not official Dispatcharr"
      >
        <Text size="sm">
          {status.build} runs on Dispatcharr {status.dispatcharr_version}. Its
          changes are not the Dispatcharr developers&apos; and they do not
          support them. If something goes wrong, uninstall it here and try the
          same thing on stock Dispatcharr first. Do not report problems with
          this build on the official Dispatcharr GitHub or Discord.
        </Text>
      </Alert>

      <Table verticalSpacing={4} fz="sm" withTableBorder>
        <Table.Tbody>
          <Table.Tr>
            <Table.Td c="dimmed">Build</Table.Td>
            <Table.Td>{status.build}</Table.Td>
          </Table.Tr>
          <Table.Tr>
            <Table.Td c="dimmed">Dispatcharr</Table.Td>
            <Table.Td>{status.dispatcharr_version}</Table.Td>
          </Table.Tr>
          <Table.Tr>
            <Table.Td c="dimmed">Installed</Table.Td>
            <Table.Td>
              {status.installed
                ? `${new Date(record.installed_at).toLocaleString()} · ${LAYOUTS[record.layout] || record.layout}`
                : 'Not by the installer'}
            </Table.Td>
          </Table.Tr>
          {record.repository && (
            <Table.Tr>
              <Table.Td c="dimmed">Where it comes from</Table.Td>
              <Table.Td style={{ wordBreak: 'break-all' }}>
                {record.repository}
              </Table.Td>
            </Table.Tr>
          )}
        </Table.Tbody>
      </Table>

      {error && <Alert color="red">{error}</Alert>}
      {(done || status.uninstall_requested) && (
        <Alert color="blue">
          {done ||
            'Uninstalling was asked for. Stock Dispatcharr is put back as soon as the installer sees it.'}
        </Alert>
      )}

      <div>
        <Button
          color="red"
          variant="light"
          leftSection={<Undo2 size={16} />}
          disabled={!status.installed || status.uninstall_requested}
          onClick={() => setAsking(true)}
        >
          Uninstall and go back to stock Dispatcharr
        </Button>
        {!status.installed && (
          <Text size="xs" c="dimmed" mt={6}>
            This build was not put here by the installer, so it cannot be taken
            out from here.
          </Text>
        )}
      </div>

      <ConfirmationDialog
        opened={asking}
        onClose={() => setAsking(false)}
        onConfirm={uninstall}
        title="Go back to stock Dispatcharr?"
        message="Every file the modified build changed is put back as it was, and Dispatcharr restarts. Your channels, streams and settings stay: stock Dispatcharr simply ignores the settings only this build uses, and they are there again if it is installed again. Streams parked by Stream Check stay off their channels, and channels it hid stay hidden: stock cannot put them back by itself, so put back what you want first, or show those channels again under Channels."
        confirmLabel="Uninstall"
      />
    </Stack>
  );
};

export default ModifiedBuild;
