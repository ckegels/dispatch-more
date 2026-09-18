import React, { useState } from 'react';
import { Box, Flex, Tabs, TabsList, TabsTab, Text } from '@mantine/core';
import ChannelManagerTable from '../components/tables/ChannelManagerTable';
import StreamCheckTable from '../components/tables/StreamCheckTable';

const BLURB = {
  merge: 'the same channel, from every provider and in every quality, as one',
  check: 'the streams on your channels that no longer play',
};

// Headed the way the Logo Manager is, tabs and all, so the two read as parts of one set of
// tools
const ChannelManagerPage = () => {
  const [activeTab, setActiveTab] = useState('merge');

  return (
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
              {BLURB[activeTab]}
            </Text>
          </Flex>
          <Tabs value={activeTab} onChange={setActiveTab} variant="pills">
            <TabsList>
              <TabsTab value="merge">Merge</TabsTab>
              <TabsTab value="check">Stream Check</TabsTab>
            </TabsList>
          </Tabs>
        </Flex>
      </Box>
      {activeTab === 'merge' ? <ChannelManagerTable /> : <StreamCheckTable />}
    </Box>
  );
};

export default ChannelManagerPage;
