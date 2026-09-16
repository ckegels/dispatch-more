import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  MultiSelect,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';

const emptyTuner = {
  channel_profile: '',
  new_profile_name: '',
  group_ids: [],
  output_profile_id: '',
};

const MediaServerTuners = ({ serverId, enabled }) => {
  const [data, setData] = useState(null);
  const [form, setForm] = useState(emptyTuner);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const [baseUrl, setBaseUrl] = useState('');

  const load = useCallback(async () => {
    if (!enabled) return;
    try {
      const result = await API.getMediaServerTuners(serverId);
      setData(result);
      // Only until it has been edited: after that the field is what the user typed
      setBaseUrl((current) => current || result.base_url || '');
    } catch {
      setError('Could not read the tuners from this server.');
    }
  }, [serverId, enabled]);

  useEffect(() => {
    load();
  }, [load]);

  const run = async (call) => {
    setBusy(true);
    setError(null);
    try {
      setData(await call());
      setForm(emptyTuner);
    } catch (e) {
      setError(e?.body?.error || 'The media server refused that.');
    } finally {
      setBusy(false);
    }
  };

  if (!enabled) return null;
  if (!data) return <Loader size="xs" />;

  const buildingNew = !form.channel_profile;

  return (
    <Stack gap="xs" mt="sm">
      <Text size="sm" fw={600}>
        Tuners
      </Text>
      {error && <Alert color="red">{error}</Alert>}

      {data.tuners.length === 0 ? (
        <Text size="sm" c="dimmed">
          This server has no tuners.
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={640} type="native">
          <Table fz="sm" verticalSpacing={4}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Tuner</Table.Th>
                <Table.Th>Address</Table.Th>
                <Table.Th w={190}>State</Table.Th>
                <Table.Th w={150} />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data.tuners.map((tuner) => (
                <Table.Tr key={tuner.id}>
                  <Table.Td>
                    {tuner.title}
                    {tuner.ours && (
                      <Badge size="xs" color="teal" variant="light" ml={6}>
                        Dispatcharr
                      </Badge>
                    )}
                  </Table.Td>
                  <Table.Td c="dimmed" style={{ wordBreak: 'break-all' }}>
                    {tuner.uri}
                  </Table.Td>
                  <Table.Td>
                    <Group gap={4} wrap="wrap">
                      <Badge
                        size="xs"
                        variant="light"
                        color={tuner.state === 'alive' ? 'teal' : 'red'}
                      >
                        {tuner.state || 'unknown'}
                      </Badge>
                      {!tuner.dvr_id && (
                        // A tuner outside a DVR does nothing: usually a leftover
                        <Badge size="xs" color="yellow" variant="light">
                          not in a DVR
                        </Badge>
                      )}
                      {tuner.tuners > 0 && (
                        <Text size="xs" c="dimmed">
                          {tuner.tuners} tuners
                        </Text>
                      )}
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    <Group gap={4} wrap="nowrap">
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        disabled={busy}
                        onClick={() =>
                          run(() =>
                            API.syncMediaServerTuner(
                              serverId,
                              tuner.id,
                              tuner.dvr_id
                            )
                          )
                        }
                      >
                        Sync
                      </Button>
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        color="red"
                        disabled={busy}
                        onClick={() =>
                          run(() =>
                            API.deleteMediaServerTuner(serverId, tuner.id)
                          )
                        }
                      >
                        Remove
                      </Button>
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}

      <Text size="xs" c="dimmed">
        Sync rescans the tuner&apos;s channels and reloads the guide of its DVR.
        Removing a tuner only removes it from the media server.
      </Text>

      <Text size="sm" fw={600} mt="xs">
        Add a tuner
      </Text>
      <Group align="flex-end" gap="xs" wrap="wrap">
        <TextInput
          size="xs"
          w={260}
          label="Dispatcharr address"
          description="How this server reaches Dispatcharr"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.currentTarget.value)}
        />
        <Select
          size="xs"
          w={220}
          label="Channel profile"
          description="What the tuner offers"
          placeholder="Build a new one"
          clearable
          value={form.channel_profile}
          onChange={(value) =>
            setForm({ ...form, channel_profile: value || '' })
          }
          data={data.channel_profiles.map((profile) => ({
            value: profile.name,
            label: profile.name,
          }))}
        />
        {buildingNew && (
          <>
            <TextInput
              size="xs"
              w={200}
              label="New profile name"
              description={`Saved as ${data.profile_prefix}-…`}
              value={form.new_profile_name}
              onChange={(e) =>
                setForm({ ...form, new_profile_name: e.currentTarget.value })
              }
            />
            <MultiSelect
              size="xs"
              w={260}
              label="Channel groups"
              description="Only these channels go in it"
              searchable
              value={form.group_ids}
              onChange={(value) => setForm({ ...form, group_ids: value })}
              data={data.channel_groups.map((group) => ({
                value: String(group.id),
                label: `${group.name} (${group.channels})`,
              }))}
            />
          </>
        )}
        <Select
          size="xs"
          w={200}
          label="Output profile"
          description="How it is sent"
          placeholder="As it comes in"
          clearable
          value={form.output_profile_id}
          onChange={(value) =>
            setForm({ ...form, output_profile_id: value || '' })
          }
          data={data.output_profiles.map((profile) => ({
            value: String(profile.id),
            label: profile.name,
          }))}
        />
        <Button
          size="compact-sm"
          loading={busy}
          onClick={() =>
            run(() =>
              API.addMediaServerTuner({
                server: serverId,
                base_url: baseUrl,
                channel_profile: form.channel_profile,
                new_profile_name: form.new_profile_name,
                group_ids: form.group_ids.map(Number),
                output_profile_id: form.output_profile_id || null,
              })
            )
          }
        >
          Add to server
        </Button>
      </Group>
      <Text size="xs" c="dimmed">
        A new profile holds only the channels of the groups you pick, and
        nothing is added to your other profiles. It is saved as{' '}
        {data.profile_prefix}-… with spaces as dashes, because the name becomes
        part of the tuner&apos;s address.
      </Text>
    </Stack>
  );
};

export default MediaServerTuners;
