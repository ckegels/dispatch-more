import React, { useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  FileInput,
  Group,
  Loader,
  Modal,
  SimpleGrid,
  Stack,
  Tabs,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';
import { sourceColor } from './logoLibraryColors';

// The name as a search would want it: what the channel is called, without the box of
// country in front that no collection repeats
const searchableName = (name) =>
  String(name || '')
    .replace(/[┃|[(][^┃|\])]*[┃|\])]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

const Result = ({ result, onChoose }) => (
  <Box
    role="button"
    aria-label={`Use ${result.name}`}
    onClick={() => onChoose(result)}
    style={{
      cursor: 'pointer',
      borderRadius: 6,
      padding: 6,
      background: 'rgba(128,128,128,0.12)',
    }}
  >
    <Box
      style={{
        height: 54,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <img
        src={result.url}
        alt=""
        loading="lazy"
        style={{ maxWidth: '100%', maxHeight: 54, objectFit: 'contain' }}
      />
    </Box>
    <Text size="xs" lineClamp={1} mt={4}>
      {result.name}
    </Text>
    <Group gap={4}>
      <Badge size="xs" variant="light" color={sourceColor(result.source)}>
        {result.source}
      </Badge>
      {result.country && (
        <Badge size="xs" variant="outline" color="gray">
          {result.country}
        </Badge>
      )}
    </Group>
  </Box>
);

/*
 * Choosing a logo for one channel by hand: searching every logo in the collections, a
 * link to one anywhere, or a file. What is chosen is handed back, not applied: it goes on
 * the list with the rest and is applied with them.
 */
const LogoPicker = ({ row, opened, onClose, onChoose }) => {
  const [tab, setTab] = useState('search');
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [searching, setSearching] = useState(false);
  const [link, setLink] = useState('');
  const [file, setFile] = useState(null);
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);

  // Each time it opens, it starts from the channel's own name
  useEffect(() => {
    if (!opened || !row) return;
    setTab('search');
    setQuery(searchableName(row.name));
    setLink('');
    setFile(null);
    setError(null);
  }, [opened, row]);

  // Searched as it is typed, but not on every letter
  useEffect(() => {
    if (!opened || tab !== 'search') return undefined;
    if (query.trim().length < 2) {
      setResults([]);
      return undefined;
    }
    const timer = setTimeout(async () => {
      setSearching(true);
      try {
        const data = await API.searchLogoLibrary(query, row?.country || '');
        setResults(data.results || []);
        setError(null);
      } catch (e) {
        setError(e?.body?.error || 'Could not search the logos.');
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [query, opened, tab, row]);

  const useLink = () => {
    const url = link.trim();
    if (!/^https?:\/\//i.test(url)) {
      setError('A link starts with http:// or https://');
      return;
    }
    onChoose({ url, name: searchableName(row.name), source: 'link' });
  };

  const useFile = async () => {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const logo = await API.uploadLogo(file, searchableName(row.name));
      // Kept on disk rather than at an address, so it is given to the channel by id
      onChoose({
        url: logo.cache_url || logo.url,
        name: logo.name,
        source: 'upload',
        logo_id: logo.id,
      });
    } catch (e) {
      setError(
        e?.body?.error || 'Could not upload that file. Is it an image?'
      );
    } finally {
      setUploading(false);
    }
  };

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={row ? `A logo for ${row.name}` : ''}
      size="xl"
      centered
    >
      <Tabs value={tab} onChange={setTab}>
        <Tabs.List>
          <Tabs.Tab value="search">Search the collections</Tabs.Tab>
          <Tabs.Tab value="link">Use a link</Tabs.Tab>
          <Tabs.Tab value="upload">Upload a file</Tabs.Tab>
        </Tabs.List>

        {error && (
          <Alert color="red" mt="sm">
            {error}
          </Alert>
        )}

        <Tabs.Panel value="search" pt="sm">
          <Stack gap="sm">
            <TextInput
              aria-label="Search logos"
              placeholder="A channel name, or part of one"
              value={query}
              onChange={(event) => setQuery(event.currentTarget.value)}
              rightSection={searching ? <Loader size="xs" /> : null}
            />
            {results.length === 0 && query.trim().length >= 2 && !searching ? (
              <Text size="sm" c="dimmed">
                Nothing in the collections by that name. A shorter part of it, or
                another name the channel goes by, can find what the whole did not.
              </Text>
            ) : (
              <SimpleGrid cols={{ base: 3, sm: 5 }} spacing="xs">
                {results.map((result) => (
                  <Result key={result.url} result={result} onChoose={onChoose} />
                ))}
              </SimpleGrid>
            )}
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="link" pt="sm">
          <Stack gap="sm">
            <TextInput
              aria-label="Logo link"
              placeholder="https://…/logo.png"
              value={link}
              onChange={(event) => setLink(event.currentTarget.value)}
            />
            {/^https?:\/\//i.test(link.trim()) && (
              <Box
                style={{
                  height: 80,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: 'rgba(128,128,128,0.12)',
                  borderRadius: 6,
                }}
              >
                <img
                  src={link.trim()}
                  alt="preview"
                  style={{ maxHeight: 72, maxWidth: '60%', objectFit: 'contain' }}
                />
              </Box>
            )}
            <Group justify="flex-end">
              <Button onClick={useLink} disabled={!link.trim()}>
                Use this link
              </Button>
            </Group>
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="upload" pt="sm">
          <Stack gap="sm">
            <FileInput
              aria-label="Logo file"
              placeholder="Choose an image"
              accept="image/png,image/jpeg,image/svg+xml,image/webp,image/gif"
              value={file}
              onChange={setFile}
            />
            {/* Learned the hard way: a media server hands a logo's address to whoever is
                watching, and an uploaded one is only at Dispatcharr's own address */}
            <Text size="xs" c="dimmed">
              An uploaded logo is kept by Dispatcharr and served from its own
              address. That is fine for players on your network, but Plex and
              Jellyfin hand that address to whoever is watching, so a phone or TV
              away from home will not show it. A link to a logo on the internet
              works everywhere.
            </Text>
            <Group justify="flex-end">
              <Button onClick={useFile} disabled={!file} loading={uploading}>
                Upload and use
              </Button>
            </Group>
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Modal>
  );
};

export default LogoPicker;
