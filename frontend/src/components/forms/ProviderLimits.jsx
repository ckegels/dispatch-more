import React, { useState } from 'react';
import { Button, Group, NumberInput, Stack, Text } from '@mantine/core';

// What each provider allows: how many streams may be opened in a stretch of minutes before
// it refuses. Learned by Stream Check the first time a provider stops giving streams, or
// typed in here -- which spares a provider even that first refusal.

const ProviderLimit = ({ row, onSave }) => {
  const [limit, setLimit] = useState(row.limit ?? '');
  const [minutes, setMinutes] = useState(row.window_minutes ?? 10);
  return (
    <Group gap="xs" wrap="wrap" align="flex-end">
      <Stack gap={0} style={{ flex: '1 1 220px', minWidth: 0 }}>
        <Text size="sm" style={{ wordBreak: 'break-word' }}>
          {row.name}
        </Text>
        <Text size="xs" c={row.resting ? 'orange.5' : 'dimmed'}>
          {row.said}
        </Text>
      </Stack>
      <NumberInput
        size="xs"
        w={90}
        min={1}
        aria-label={`Streams for ${row.name}`}
        placeholder="streams"
        value={limit}
        onChange={setLimit}
      />
      <Text size="xs" c="dimmed">
        every
      </Text>
      <NumberInput
        size="xs"
        w={80}
        min={1}
        aria-label={`Minutes for ${row.name}`}
        value={minutes}
        onChange={setMinutes}
      />
      <Text size="xs" c="dimmed">
        min
      </Text>
      <Button
        size="compact-xs"
        variant="light"
        disabled={!limit}
        onClick={() => onSave(row, Number(limit), Number(minutes) || 10)}
      >
        Set
      </Button>
      {row.limit && (
        <Button
          size="compact-xs"
          variant="subtle"
          color="gray"
          onClick={() => onSave(row, null, null)}
        >
          Forget
        </Button>
      )}
    </Group>
  );
};

const ProviderLimits = ({ limits, onSave }) => (
  <Stack gap="xs">
    <Text size="xs" fw={700} tt="uppercase" c="dimmed">
      What each provider allows
    </Text>
    <Text size="xs" c="dimmed">
      Many providers refuse every stream for a while after a number of them are
      opened in a row, even one at a time. Stream Check learns that number the
      first time it is hit and stays under it from then on. Typing it in here
      spares the provider even that first refusal; Forget has it learned again.
    </Text>
    {(limits || []).map((row) => (
      <ProviderLimit
        key={`${row.key}-${row.limit}-${row.window_minutes}`}
        row={row}
        onSave={onSave}
      />
    ))}
  </Stack>
);

export default ProviderLimits;
