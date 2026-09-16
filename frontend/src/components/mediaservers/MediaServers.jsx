import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Loader,
  PasswordInput,
  Stack,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';

const REFRESH_MS = 10000;

const emptyForm = { id: null, name: '', url: '', token: '' };

const MediaServers = ({ active }) => {
  const [servers, setServers] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

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
              <Text fw={600}>{server.name}</Text>
              <Text size="sm" c="dimmed">
                {server.url}
              </Text>
              {server.online ? (
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
              <Button
                size="compact-sm"
                variant="subtle"
                onClick={() =>
                  setForm({
                    id: server.id,
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
                          {session.speed ? ` ${session.speed.toFixed(1)}×` : ''}
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
        </Card>
      ))}

      <Card withBorder padding="sm">
        <Stack gap="xs">
          <Text fw={600} size="sm">
            {form.id ? 'Edit media server' : 'Add a media server'}
          </Text>
          <TextInput
            label="Name"
            placeholder="Plex"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.currentTarget.value })}
          />
          <TextInput
            label="Address"
            description="The local address of the server, for example http://192.168.2.141:32400"
            placeholder="http://192.168.2.141:32400"
            value={form.url}
            onChange={(e) => setForm({ ...form, url: e.currentTarget.value })}
          />
          <PasswordInput
            label="Token"
            description={
              form.id
                ? 'Leave empty to keep the token that is stored'
                : 'The X-Plex-Token of your server. It is stored on the server and never shown again.'
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
