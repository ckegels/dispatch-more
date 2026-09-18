import React from 'react';
import { Box, Flex, Text } from '@mantine/core';
import ChannelManagerTable from '../components/tables/ChannelManagerTable';

// Headed the way the Logo Manager is, so the two read as parts of one set of tools
const ChannelManagerPage = () => (
  <Box>
    <Box style={{ justifyContent: 'center' }} display={'flex'} p={'10px 0'}>
      <Flex
        style={{ alignItems: 'center', justifyContent: 'space-between' }}
        w={'100%'}
        maw={'1200px'}
        pb={10}
      >
        <Flex gap={8} align="center">
          <Text
            ff={'Inter, sans-serif'}
            fz={'20px'}
            fw={500}
            lh={1}
            c="white"
            mb={0}
            lts={'-0.3px'}
          >
            Channel Manager
          </Text>
          <Text size="sm" c="dimmed">
            the same channel, from every provider and in every quality, as one
          </Text>
        </Flex>
      </Flex>
    </Box>
    <ChannelManagerTable />
  </Box>
);

export default ChannelManagerPage;
