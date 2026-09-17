import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Collapse,
  Group,
  Loader,
  PasswordInput,
  Select,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';
import MediaServerTuners from './MediaServerTuners';

const REFRESH_MS = 10000;

const emptyForm = { id: null, kind: 'plex', name: '', url: '', token: '' };

const MediaServers = ({ active }) => {
  const [servers, setServers] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  // A server's details fold away: with several of them the page is mostly tables
  const [openServers, setOpenServers] = useState({});

  const load = useCallback(async () => {
    try {
      const data = await API.getMediaServers();
      setServers(data.servers);
    } catch {
      setError('Could not load the media servers.');
    }
  }, []);

  useEffect(() => {
    if (!active) return undefined;
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [active, load]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const data = await API.saveMediaServer(form);
      setServers(data.servers);
      setForm(emptyForm);
    } catch (e) {
      // The server is only saved when it answers, so this is worth showing as it is
      setError(
        e?.body?.error || 'Could not reach that server with this token.'
      );
    } finally {
      setSaving(false);
    }
  };

  const setEnabled = async (id, enabled) => {
    try {
      const data = await API.setMediaServerEnabled(id, enabled);
      setServers(data.servers);
    } catch {
      setError('Could not change that server.');
    }
  };

  const remove = async (id) => {
    try {
      const data = await API.deleteMediaServer(id);
      setServers(data.servers);
    } catch {
      setError('Could not remove that server.');
    }
  };

  if (!servers) {
    return <Loader size="sm" />;
  }

  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">
        A media server is read only: Dispatcharr asks Plex what it is playing
        and how, and adds that to the Diagnostics page, so you can see what
        happens to a stream after Dispatcharr hands it over. Nothing is changed
        on the server.
      </Text>

      {error && <Alert color="red">{error}</Alert>}

      {servers.map((server) => (
        <Card key={server.id} withBorder padding="sm">
          <Group justify="space-between" wrap="wrap">
            <Group gap="xs" wrap="wrap">
              <Button
                size="compact-xs"
                variant="subtle"
                color="gray"
                aria-label={`${openServers[server.id] ? 'Hide' : 'Show'} ${server.name}`}
                onClick={() =>
                  setOpenServers({
                    ...openServers,
                    [server.id]: !openServers[server.id],
                  })
                }
              >
                {openServers[server.id] ? '▾' : '▸'}
              </Button>
              <Text fw={600}>{server.name}</Text>
              <Badge size="sm" variant="light" color="gray">
                {server.kind === 'jellyfin' ? 'Jellyfin' : 'Plex'}
              </Badge>
              <Text size="sm" c="dimmed">
                {server.url}
              </Text>
              {!server.enabled ? (
                <Badge size="sm" color="gray" variant="light">
                  switched off
                </Badge>
              ) : server.online ? (
                <Badge size="sm" color="teal" variant="light">
                  connected{server.version ? ` · ${server.version}` : ''}
                </Badge>
              ) : (
                <Badge size="sm" color="red" variant="light">
                  {server.error || 'not reachable'}
                </Badge>
              )}
            </Group>
            <Group gap="xs">
              <Switch
                size="sm"
                checked={server.enabled}
                aria-label={`Use ${server.name}`}
                onChange={(event) =>
                  setEnabled(server.id, event.currentTarget.checked)
                }
              />
              <Button
                size="compact-sm"
                variant="subtle"
                onClick={() =>
                  setForm({
                    id: server.id,
                    kind: server.kind || 'plex',
                    name: server.name,
                    url: server.url,
                    token: '',
                  })
                }
              >
                Edit
              </Button>
              <Button
                size="compact-sm"
                variant="subtle"
                color="red"
                onClick={() => remove(server.id)}
              >
                Remove
              </Button>
            </Group>
          </Group>

          <Collapse in={!!openServers[server.id]}>
            {server.online &&
              (server.sessions.length === 0 ? (
                <Text size="sm" c="dimmed" mt="xs">
                  Nothing is playing.
                </Text>
              ) : (
                <Table mt="xs" fz="sm" verticalSpacing={4}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Watching</Table.Th>
                      <Table.Th>Who</Table.Th>
                      <Table.Th>Player</Table.Th>
                      <Table.Th>How</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {server.sessions.map((session) => (
                      <Table.Tr key={`${session.user}-${session.title}`}>
                        <Table.Td>{session.title}</Table.Td>
                        <Table.Td>{session.user}</Table.Td>
                        <Table.Td c="dimmed">{session.player}</Table.Td>
                        <Table.Td>
                          <Badge
                            size="sm"
                            variant="light"
                            color={
                              session.decision === 'direct play'
                                ? 'teal'
                                : 'orange'
                            }
                          >
                            {session.decision}
                            {session.speed
                              ? ` ${session.speed.toFixed(1)}×`
                              : ''}
                          </Badge>
                          {session.state === 'buffering' && (
                            <Badge
                              size="sm"
                              color="yellow"
                              variant="light"
                              ml={6}
                            >
                              buffering
                            </Badge>
                          )}
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              ))}

            {openServers[server.id] && (
              <MediaServerTuners
                serverId={server.id}
                enabled={server.enabled}
              />
            )}
          </Collapse>
        </Card>
      ))}

      <Card withBorder padding="sm">
        <Stack gap="xs">
          <Text fw={600} size="sm">
            {form.id ? 'Edit media server' : 'Add a media server'}
          </Text>
          <Select
            label="Kind"
            description="How Dispatcharr talks to it"
            value={form.kind}
            onChange={(value) => setForm({ ...form, kind: value || 'plex' })}
            data={[
              { value: 'plex', label: 'Plex' },
              { value: 'jellyfin', label: 'Jellyfin' },
            ]}
          />
          <TextInput
            label="Name"
            placeholder={form.kind === 'jellyfin' ? 'Jellyfin' : 'Plex'}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.currentTarget.value })}
          />
          <TextInput
            label="Address"
            description={
              form.kind === 'jellyfin'
                ? 'The local address of the server, for example http://192.168.2.141:8096'
                : 'The local address of the server, for example http://192.168.2.141:32400'
            }
            placeholder={
              form.kind === 'jellyfin'
                ? 'http://192.168.2.141:8096'
                : 'http://192.168.2.141:32400'
            }
            value={form.url}
            onChange={(e) => setForm({ ...form, url: e.currentTarget.value })}
          />
          <PasswordInput
            label={form.kind === 'jellyfin' ? 'API key' : 'Token'}
            description={
              form.id
                ? 'Leave empty to keep the one that is stored'
                : form.kind === 'jellyfin'
                  ? 'Dashboard → API Keys → add one. Stored on the server, never shown again.'
                  : 'The X-Plex-Token of your server. Stored on the server, never shown again.'
            }
            value={form.token}
            onChange={(e) => setForm({ ...form, token: e.currentTarget.value })}
          />
          <Group>
            <Button onClick={save} loading={saving}>
              {form.id ? 'Save' : 'Add'}
            </Button>
            {form.id && (
              <Button variant="subtle" onClick={() => setForm(emptyForm)}>
                Cancel
              </Button>
            )}
          </Group>
        </Stack>
      </Card>
    </Stack>
  );
};

export default MediaServers;
