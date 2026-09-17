import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  MultiSelect,
  NumberInput,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';
import ConfirmationDialog from '../ConfirmationDialog';

const emptyTuner = {
  channel_profile: '',
  new_profile_name: '',
  group_ids: [],
  output_profile_id: '',
  tuner_count: '',
  dvr_id: '',
  language: 'eng',
  tuner_type: 'hdhomerun',
};

const MediaServerTuners = ({ serverId, enabled }) => {
  const [data, setData] = useState(null);
  const [form, setForm] = useState(emptyTuner);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const [baseUrl, setBaseUrl] = useState('');
  // Removing a tuner or a guide cannot be undone from here, so it is asked first
  const [confirming, setConfirming] = useState(null);

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
      const result = await call();
      setData(result);
      setForm(emptyTuner);
      // Something worked but not everything: worth saying, without looking like a failure
      if (result.warning) setError(result.warning);
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
                      {/* Jellyfin does not say whether a tuner answered, so nothing is said */}
                      {tuner.state && (
                        <Badge
                          size="xs"
                          variant="light"
                          color={tuner.state === 'alive' ? 'teal' : 'red'}
                        >
                          {tuner.state}
                        </Badge>
                      )}
                      {!tuner.dvr_id && (
                        // A tuner outside a DVR is registered but not used at all
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
                      {tuner.dvr_id ? (
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
                      ) : (
                        // Nothing can be done with it until it is in a DVR
                        <Select
                          size="xs"
                          w={130}
                          aria-label={`Add ${tuner.title} to a DVR`}
                          placeholder="Add to a DVR"
                          value={null}
                          onChange={(value) =>
                            value &&
                            run(() =>
                              API.attachMediaServerTuner(
                                serverId,
                                tuner.id,
                                value
                              )
                            )
                          }
                          data={(data.dvrs || []).map((dvr) => ({
                            value: dvr.id,
                            label: dvr.title,
                          }))}
                        />
                      )}
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        color="red"
                        disabled={busy}
                        onClick={() =>
                          setConfirming({
                            title: 'Remove this tuner?',
                            message: `"${tuner.title}" is removed from this media server. Dispatcharr and its channels are not touched, and it can be added again.`,
                            confirmLabel: 'Remove tuner',
                            action: () =>
                              API.deleteMediaServerTuner(serverId, tuner.id),
                          })
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
        Adding a tuner puts it straight into a DVR: a new one, or an existing
        one, which keeps the tuners it already has and gains this one. Either
        way its guide is Dispatcharr&apos;s own EPG for that channel profile.
        Its channels are then scanned and the guide loaded, so it is ready to
        watch: its channels are scanned, switched on and mapped to the guide,
        because a channel the server found but left switched off never appears.
        Sync does that again later. Removing a tuner only removes it from the
        media server.
      </Text>

      {(data.dvrs || []).length > 0 && (
        <>
          <Text size="sm" fw={600} mt="xs">
            DVRs
          </Text>
          {data.dvrs.map((dvr) => (
            <Group key={dvr.id} gap="xs" wrap="wrap">
              <Text size="sm">{dvr.title}</Text>
              <Text size="xs" c="dimmed">
                {dvr.tuners.length > 0
                  ? dvr.tuners.join(', ')
                  : 'no tuners in it'}
                {dvr.lineups.length > 0 &&
                  ` · guides: ${dvr.lineups.join(', ')}`}
              </Text>
              <Button
                size="compact-xs"
                variant="subtle"
                color="red"
                disabled={busy}
                onClick={() =>
                  setConfirming({
                    title: 'Remove this DVR?',
                    message: `"${dvr.title}" is removed from this media server, with the guide it uses. Its tuners stay registered, outside any DVR, and its recordings are not touched.`,
                    confirmLabel: 'Remove DVR',
                    action: () => API.deleteMediaServerDvr(serverId, dvr.id),
                  })
                }
              >
                Remove DVR
              </Button>
            </Group>
          ))}
          <Text size="xs" c="dimmed">
            Removing a DVR leaves its tuners registered on the server, outside
            any DVR, so they can be put in another one.
          </Text>
        </>
      )}

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
        {data.kind === 'jellyfin' && (
          <Select
            size="xs"
            w={150}
            label="Added as"
            description="How it reads the channels"
            value={form.tuner_type}
            onChange={(value) =>
              setForm({ ...form, tuner_type: value || 'hdhomerun' })
            }
            data={[
              { value: 'hdhomerun', label: 'HDHomeRun tuner' },
              { value: 'm3u', label: 'M3U playlist' },
            ]}
          />
        )}
        <NumberInput
          size="xs"
          w={130}
          label="Tuners"
          description={`Dispatcharr says ${data.calculated_tuners}`}
          placeholder="as calculated"
          min={1}
          max={data.max_tuners}
          value={form.tuner_count}
          onChange={(value) => setForm({ ...form, tuner_count: value })}
        />
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
        <Select
          size="xs"
          w={190}
          label="DVR"
          description="Added to it, next to its other tuners"
          placeholder="Make a new DVR"
          clearable
          value={form.dvr_id}
          onChange={(value) => setForm({ ...form, dvr_id: value || '' })}
          data={(data.dvrs || []).map((dvr) => ({
            value: dvr.id,
            label: dvr.title,
          }))}
        />
        {!form.dvr_id && (
          <Select
            size="xs"
            w={160}
            label="Language"
            description="For the guide"
            searchable
            // "fr" and "French" both find French, which the server wants as "fre"
            value={form.language}
            onChange={(value) => setForm({ ...form, language: value || 'eng' })}
            data={data.languages || []}
          />
        )}
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
                tuner_count: form.tuner_count || 0,
                dvr_id: form.dvr_id,
                language: form.language,
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
        part of the tuner&apos;s address. Tuners is how many streams the media
        server may start at once: set it to what your providers really allow,
        because Dispatcharr&apos;s own number counts a custom stream per channel
        as a tuner.
      </Text>
      <ConfirmationDialog
        opened={!!confirming}
        onClose={() => setConfirming(null)}
        onConfirm={() => {
          const { action } = confirming;
          setConfirming(null);
          run(action);
        }}
        title={confirming?.title}
        message={confirming?.message}
        confirmLabel={confirming?.confirmLabel}
      />
    </Stack>
  );
};

export default MediaServerTuners;
