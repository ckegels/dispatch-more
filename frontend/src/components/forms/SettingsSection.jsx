import React, { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { Box, SimpleGrid, Text, UnstyledButton } from '@mantine/core';

// Every setting carries a sentence or two saying what it does, and all of them at once
// is a wall nobody reads. Shut, each section is one line saying what it is for; open, its
// settings get two columns of room instead of four cramped ones. Stream Check's settings
// were the first laid out this way, the Lineup's levers the second.
const SettingsSection = ({ title, about, children, openAtFirst = false }) => {
  const [open, setOpen] = useState(openAtFirst);
  return (
    <Box
      style={{
        border: '1px solid #3f3f46',
        borderRadius: 'var(--mantine-radius-sm)',
      }}
    >
      <UnstyledButton
        onClick={() => setOpen(!open)}
        aria-label={`${open ? 'Close' : 'Open'} ${title}`}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          width: '100%',
          padding: '8px 12px',
        }}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <Text size="xs" fw={700} tt="uppercase" c="dimmed">
          {title}
        </Text>
        <Text size="xs" c="dimmed" style={{ minWidth: 0 }}>
          {about}
        </Text>
      </UnstyledButton>
      {open && (
        <Box p="md" pt={0}>
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
            {children}
          </SimpleGrid>
        </Box>
      )}
    </Box>
  );
};

export default SettingsSection;
