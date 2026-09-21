import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Select,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';
import ConfirmationDialog from '../ConfirmationDialog';

// What each kind of collection is, in the words someone adding one would use, and what
// its link looks like, since that is the thing most easily got wrong
const KINDS = {
  github: {
    label: 'GitHub repository of images',
    placeholder: 'owner/repository, or its GitHub link',
  },
  m3u: {
    label: 'M3U playlist (tvg-logo)',
    placeholder: 'https://…/playlist.m3u',
  },
  xmltv: {
    label: 'XMLTV guide (channel icons)',
    placeholder: 'https://…/guide.xml',
  },
  json: {
    label: 'JSON list of logos',
    placeholder: 'https://…/logos.json',
  },
  // For the places that publish files rather than a list: a page of guides, a folder of
  // picons. The page is the only address there is, so the page is what you give it.
  page: {
    label: 'A page that links to guides or images',
    placeholder: 'https://www.epg.guru/',
  },
};

const emptySource = { type: 'github', url: '', name: '' };

/*
 * The collections logos are looked for in: the two built in, and any added here. Adding
 * one reads it first, so a link that holds nothing, or is not the kind of thing it was said
 * to be, is found out now rather than being a collection that quietly contributes nothing.
 */
const LogoSources = ({ onChanged }) => {
  const [data, setData] = useState(null);
  const [form, setForm] = useState(emptySource);
  const [checked, setChecked] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(null);

  const load = useCallback(async () => {
    try {
      setData(await API.getLogoSources());
    } catch (e) {
      setError(e?.body?.error || 'Could not load the collections.');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const run = async (call) => {
    setBusy(true);
    setError(null);
    try {
      return await call();
    } catch (e) {
      setError(e?.body?.error || 'That did not work.');
      return null;
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    setChecked(null);
    const result = await run(() => API.checkLogoSource(form));
    if (result) setChecked(result);
  };

  const add = async () => {
    const result = await run(() => API.addLogoSource(form));
    if (result) {
      setData(result);
      setForm(emptySource);
      setChecked(null);
      onChanged && onChanged();
    }
  };

  const toggle = async (source, enabled) => {
    const result = await run(() => API.setLogoSourceEnabled(source.id, enabled));
    if (result) {
      setData(result);
      onChanged && onChanged();
    }
  };

  const remove = async () => {
    const source = removing;
    setRemoving(null);
    const result = await run(() => API.removeLogoSource(source.id));
    if (result) {
      setData(result);
      onChanged && onChanged();
    }
  };

  if (!data) return null;

  return (
    <Stack gap="xs">
      {error && <Alert color="red">{error}</Alert>}

      <Table.ScrollContainer minWidth={640} type="native">
        <Table withTableBorder verticalSpacing={4} fz="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Collection</Table.Th>
              <Table.Th w={190}>Kind</Table.Th>
              <Table.Th w={110}>Logos</Table.Th>
              <Table.Th w={70}>On</Table.Th>
              <Table.Th w={80} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data.sources.map((source) => (
              <Table.Tr key={source.id}>
                <Table.Td style={{ wordBreak: 'break-all' }}>
                  <Group gap={6}>
                    <Text size="sm">{source.name}</Text>
                    {source.built_in && (
                      <Badge size="xs" variant="light" color="gray">
                        built in
                      </Badge>
                    )}
                  </Group>
                  <Text size="xs" c="dimmed">
                    {source.url}
                  </Text>
                  {source.error && (
                    <Text size="xs" c="orange">
                      Last time: {source.error}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td c="dimmed">
                  {KINDS[source.type]?.label || source.type}
                </Table.Td>
                <Table.Td c="dimmed">
                  {source.count != null ? source.count.toLocaleString() : '—'}
                </Table.Td>
                <Table.Td>
                  <Switch
                    size="xs"
                    aria-label={`Use ${source.name}`}
                    checked={!!source.enabled}
                    disabled={busy}
                    onChange={(event) =>
                      toggle(source, event.currentTarget.checked)
                    }
                  />
                </Table.Td>
                <Table.Td>
                  {!source.built_in && (
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      color="red"
                      aria-label={`Remove ${source.name}`}
                      onClick={() => setRemoving(source)}
                    >
                      Remove
                    </Button>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>

      <Text size="sm" fw={600} mt="xs">
        Add a collection
      </Text>
      <Group gap="xs" align="flex-end" wrap="wrap">
        <Select
          size="xs"
          w={230}
          label="Kind"
          aria-label="Kind of collection"
          value={form.type}
          allowDeselect={false}
          onChange={(value) => {
            setForm({ ...form, type: value || 'github' });
            setChecked(null);
          }}
          data={Object.entries(KINDS).map(([value, kind]) => ({
            value,
            label: kind.label,
          }))}
        />
        <TextInput
          size="xs"
          style={{ flex: 1, minWidth: 240 }}
          label="Link"
          aria-label="Collection link"
          placeholder={KINDS[form.type]?.placeholder}
          value={form.url}
          onChange={(event) => {
            setForm({ ...form, url: event.currentTarget.value });
            setChecked(null);
          }}
        />
        <TextInput
          size="xs"
          w={170}
          label="Name"
          aria-label="Collection name"
          placeholder="What to call it"
          value={form.name}
          onChange={(event) =>
            setForm({ ...form, name: event.currentTarget.value })
          }
        />
        <Button
          size="xs"
          variant="light"
          disabled={!form.url.trim() || busy}
          onClick={check}
        >
          Check
        </Button>
        <Button
          size="xs"
          disabled={!form.url.trim() || busy}
          loading={busy}
          onClick={add}
        >
          Add
        </Button>
      </Group>

      {checked && (
        <Stack gap={4}>
          <Text size="xs" c={checked.count ? 'teal' : 'orange'}>
            {checked.count
              ? `${checked.count.toLocaleString()} logos found. The first few:`
              : 'That was read, but there are no logos in it.'}
          </Text>
          <Group gap={6} wrap="wrap">
            {checked.sample.map((logo) => (
              <Box
                key={logo.url}
                title={logo.name}
                style={{
                  width: 56,
                  height: 36,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: 'rgba(128,128,128,0.18)',
                  borderRadius: 4,
                }}
              >
                <img
                  src={logo.url}
                  alt={logo.name}
                  loading="lazy"
                  style={{ maxWidth: '92%', maxHeight: '92%', objectFit: 'contain' }}
                />
              </Box>
            ))}
          </Group>
        </Stack>
      )}

      <Text size="xs" c="dimmed">
        A collection is read when it is added, to see what it holds, and then
        again each time the logo lists are updated. Its logos are suggested after
        tv-logos, whose links last, and before iptv-org, whose images live on
        sites that come and go. Switching one off, or removing it, takes effect
        at the next update.
      </Text>

      <ConfirmationDialog
        opened={!!removing}
        onClose={() => setRemoving(null)}
        onConfirm={remove}
        title="Remove this collection?"
        message={`Its logos stop being suggested after the next update. Logos already given to channels from it stay where they are.`}
        confirmLabel="Remove"
      />
    </Stack>
  );
};

export default LogoSources;
