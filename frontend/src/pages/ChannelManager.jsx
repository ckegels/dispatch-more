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
import EpgGrabberTable from '../components/tables/EpgGrabberTable';
import GuideLayoutTable from '../components/tables/GuideLayoutTable';
import GuideManagerTable from '../components/tables/GuideManagerTable';
import LogoLibraryTable from '../components/tables/LogoLibraryTable';
import StreamCheckTable from '../components/tables/StreamCheckTable';

// One sentence per tab, saying what the page is for. On its own line under the tabs, so
// it can be a whole sentence without moving them.
const BLURB = {
  merge:
    'Your channels as they could be: one channel per station, with every provider’s copy of it as a stream underneath, and a new channel for anything you do not have yet. Nothing changes until you apply.',
  check:
    'The streams on your channels that no longer play — opened one at a time, never while somebody is watching through that provider, and never counted against a stream on one bad look.',
  guides:
    'Which programme guide each channel should be on: the ones on no guide, the ones whose guide is empty, and the ones something else matches better.',
  logos:
    'The logo each channel has now, next to the ones the public collections would give it. Tick the ones you want and apply.',
  layout:
    'What order your channels come in and which numbers they are on. Drag one to move it, within a group or into another.',
  grabber:
    'The iptv-org/epg grabber that is installed on this machine, run from here: where it is, what to grab, and when. The guide it writes is read straight off disk, and the one you have is only replaced once the new one has been read back and found to hold something.',
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
          {/* The tabs sit in the same place on every tab. They used to share a line
              with the sentence under the title, so a longer sentence moved every tab
              along and the one you were aiming at was somewhere else on each page. */}
          <Text
            ff={'Inter, sans-serif'}
            fz={'20px'}
            fw={500}
            lh={1}
            c="white"
            mb={0}
            lts={'-0.3px'}
            style={{ flexShrink: 0 }}
          >
            Channel Manager
          </Text>
          <Tabs value={activeTab} onChange={setActiveTab} variant="pills">
            <TabsList>
              <TabsTab value="merge">Lineup</TabsTab>
              <TabsTab value="check">Stream Check</TabsTab>
              <TabsTab value="guides">Guides</TabsTab>
              <TabsTab value="logos">Logos</TabsTab>
              <TabsTab value="layout">Guide Layout</TabsTab>
              <TabsTab value="grabber">EPG Grabber</TabsTab>
            </TabsList>
          </Tabs>
        </Flex>
      </Box>
      {/* ...and the sentence goes on its own line under them, where it has room to say
          something useful instead of being kept short to leave the tabs alone */}
      <Box style={{ display: 'flex', justifyContent: 'center' }} pb="xs">
        <Text
          size="sm"
          c="dimmed"
          w="100%"
          maw="1200px"
          mx={{ base: 'xs', md: 0 }}
        >
          {BLURB[activeTab]}
        </Text>
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
            Applying a merge, removing or parking streams, or putting hundreds
            of channels on another guide, changes many channels at once, and
            there is no undo. A backup lets you put everything back as it was.{' '}
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
      {activeTab === 'layout' && <GuideLayoutTable />}
      {activeTab === 'grabber' && <EpgGrabberTable />}
    </Box>
  );
};

export default ChannelManagerPage;
