import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Anchor,
  Box,
  Flex,
  Tabs,
  TabsList,
  TabsTab,
  Text,
} from '@mantine/core';
import { DatabaseBackup } from 'lucide-react';
import ChannelManagerTable from '../components/tables/ChannelManagerTable';
import GuideManagerTable from '../components/tables/GuideManagerTable';
import LogoLibraryTable from '../components/tables/LogoLibraryTable';
import StreamCheckTable from '../components/tables/StreamCheckTable';

const BLURB = {
  merge:
    'your channels as they could be: new ones for new streams, and every copy of a channel merged into one',
  check: 'the streams on your channels that no longer play',
  guides:
    'the channels whose programme guide is missing, empty, or not the best one there is',
  logos: 'the logo each channel has, next to the ones the collections would give it',
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
          px={{ base: 'xs', md: 0 }}
          wrap="wrap"
          gap="sm"
        >
          <Flex gap={8} align="center" wrap="wrap">
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
              <TabsTab value="merge">Lineup</TabsTab>
              <TabsTab value="check">Stream Check</TabsTab>
              <TabsTab value="guides">Guides</TabsTab>
              <TabsTab value="logos">Logos</TabsTab>
            </TabsList>
          </Tabs>
        </Flex>
      </Box>
      <Box style={{ display: 'flex', justifyContent: 'center' }} pb="md">
        {/* Both tabs change channels in bulk: hundreds of them in one apply */}
        <Alert
          color="yellow"
          variant="light"
          icon={<DatabaseBackup size={18} />}
          title="Make a backup before you apply anything"
          w="100%"
          maw="1200px"
          mx={{ base: 'xs', md: 0 }}
        >
          <Text size="sm">
            Applying a merge, removing or parking streams, or putting hundreds of
            channels on another guide, changes many channels at once, and there is
            no undo. A backup lets you put everything back as it was.{' '}
            <Anchor component={Link} to="/settings#backups" size="sm" fw={600}>
              Make a backup in Settings → Backup &amp; Restore
            </Anchor>
          </Text>
        </Alert>
      </Box>
      {activeTab === 'merge' && <ChannelManagerTable />}
      {activeTab === 'check' && <StreamCheckTable />}
      {activeTab === 'guides' && <GuideManagerTable />}
      {activeTab === 'logos' && <LogoLibraryTable />}
    </Box>
  );
};

export default ChannelManagerPage;
