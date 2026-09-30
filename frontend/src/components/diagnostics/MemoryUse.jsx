import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  Stack,
  Table,
  Text,
} from '@mantine/core';
import { RefreshCw } from 'lucide-react';
import API from '../../api';

// Every Dispatcharr process and the memory it really holds (shared pages divided among the
// processes sharing them), and which ones have the language model's libraries loaded. Read
// on request only: a look costs a pass over /proc, which is cheap, but not every 5 seconds.
const MemoryUse = () => {
  const [found, setFound] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const look = useCallback(async () => {
    setBusy(true);
    try {
      setFound(await API.getMemoryUse());
      setError(null);
    } catch {
      setError('Could not read the memory use.');
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    look();
  }, [look]);

  if (error) return <Alert color="red">{error}</Alert>;
  if (!found) return <Loader size="sm" />;

  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Text size="sm">
          Dispatcharr holds <b>{Math.round(found.total_mb)} MB</b> in{' '}
          {found.processes.length} processes. The machine:{' '}
          {found.system.used_mb} of {found.system.total_mb} MB used (
          {found.system.percent} %).
        </Text>
        <Button
          size="xs"
          variant="default"
          leftSection={<RefreshCw size={14} />}
          onClick={look}
          loading={busy}
        >
          Look again
        </Button>
      </Group>
      <Table striped verticalSpacing={4} fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>What</Table.Th>
            <Table.Th>Processes</Table.Th>
            <Table.Th>Memory</Table.Th>
            <Table.Th>Language model loaded</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {found.kinds.map((kind) => (
            <Table.Tr key={kind.kind}>
              <Table.Td>{kind.kind}</Table.Td>
              <Table.Td>{kind.processes}</Table.Td>
              <Table.Td>{Math.round(kind.mb)} MB</Table.Td>
              <Table.Td>{kind.torch ? `in ${kind.torch}` : ''}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <Text size="xs" c="dimmed">
        Each process:
      </Text>
      <Table verticalSpacing={2} fz="xs">
        <Table.Tbody>
          {found.processes.map((one) => (
            <Table.Tr key={one.pid}>
              <Table.Td>{one.pid}</Table.Td>
              <Table.Td>
                {one.kind}
                {one.torch && (
                  <Badge ml={6} size="xs" color="orange" variant="light">
                    language model
                  </Badge>
                )}
              </Table.Td>
              <Table.Td>
                {Math.round(one.mb)} MB
                {!one.exact && (
                  <Text span size="xs" c="dimmed">
                    {' '}
                    (RSS)
                  </Text>
                )}
              </Table.Td>
              <Table.Td>
                <Text size="xs" c="dimmed" lineClamp={1} title={one.command}>
                  {one.command}
                </Text>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
};

export default MemoryUse;
