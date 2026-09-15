// Modal.js
import React, { useEffect, useState } from 'react';
import useUserAgentsStore from '../../store/userAgents';
import useServerGroupsStore from '../../store/serverGroups';
import usePlaylistsStore from '../../store/playlists';
import M3UProfiles from './M3UProfiles';
import {
  Box,
  Button,
  Collapse,
  Divider,
  FileInput,
  Flex,
  Group,
  LoadingOverlay,
  Modal,
  NumberInput,
  PasswordInput,
  Select,
  Stack,
  Switch,
  TextInput,
} from '@mantine/core';
import M3UGroupFilter from './M3UGroupFilter';
import useChannelsStore from '../../store/channels';
import { isNotEmpty, useForm } from '@mantine/form';
import useEPGsStore from '../../store/epgs';
import useVODStore from '../../store/useVODStore';
import M3UFilters from './M3UFilters';
import ScheduleInput from './ScheduleInput';
import { DateTimePicker } from '@mantine/dates';
import { showNotification } from '../../utils/notificationUtils.js';
import { addEPG } from '../../utils/forms/DummyEpgUtils.js';
import {
  addPlaylist,
  expDateFromPlaylist,
  expDateKey,
  getPlaylist,
  prepareSubmitValues,
  updatePlaylist,
} from '../../utils/forms/M3uUtils.js';
import ServerGroupsManagerModal from '../ServerGroupsManagerModal';
import ConfirmationDialog from '../ConfirmationDialog';

const OVERLAP_EXPLANATION = (
  <div style={{ whiteSpace: 'pre-line' }}>
    {`Only use this if your provider tolerates one extra connection for a few seconds. Some providers block accounts that go over their limit.

Warnings:
• Every device that logs in with a Dispatcharr username and password (for example an Xtream app) needs its own user. A login shared by several devices does not work with this feature: the overlap can go to the wrong device, and "Stop Skipped Channels" can stop a channel another device is watching.
• Stream links in M3U playlists change every time a player downloads the playlist. Players that remember favourites by stream link can lose them. Adding a fixed name to the playlist URL (?device_id=livingroom) keeps the links the same for that device.
• "Stop Skipped Channels" does not support multiview or picture-in-picture: a player that opens a second channel within the Overlap Window closes the first one.

What it does:
• Only when every account a channel can use is at its Max Streams, a viewer who is already watching on this account can start a new channel immediately on one temporary extra connection.
• If a stream on this account ends within the Overlap Window (a channel switch), the new stream simply continues.
• If none ends in time, the new channel moves to an account with a free slot, or to a custom fallback stream if the channel has one (such as the could-not-dispatch plugin's), or the new stream is stopped. A fallback stream at the end of a channel does not replace a channel switch.
• With "Stop Skipped Channels", channels a player only watched for a moment (shorter than the Overlap Window) are closed as soon as its next channel has started, so fast channel surfing does not fill every slot.
• When a player's channel ends during a switch, its slot is kept for that player for the Overlap Window, so another viewer waiting for a slot cannot take it in between. Failover, stream changes, VOD, catch-up and previews leave it alone too (also on a login shared through a Server Group); DVR recordings can still use it. Channels stopped from the dashboard, deleted or removed by a refresh are not kept.
• With a Channel Shutdown Delay, a channel nobody watches any more is closed early when it keeps this account over its limit during a switch.
• "When Switching Channels" chooses the account for a player's next channel. "Follow channel order" uses the channel's stream order. "Stay on same account" uses the account it is watching on or just left, with the overlap slot if its old stream is still closing, even if another account has a free slot. "Use another account" starts it on a free slot on another account with this setting enabled first (never on custom fallback streams), and only falls back to its own account when none is free.
• While any account has this enabled, M3U playlists add a device ID to their stream links so players can be recognised. Players need to re-download their playlist. Jellyfin, Emby and Plex are recognised by their User-Agent and get no device ID (unless a custom User-Agent is set in their tuner settings), so their viewers count as anonymous.

What it does not do:
• It changes nothing while an account still has free slots.
• It never stops a stream that was already playing, or a channel someone else is also watching.
• It does not give extra connections to other viewers (other users, devices or IP addresses) or to DVR recordings.
• Viewers without a user or device ID (HDHomeRun, Plex, Jellyfin, Emby) are only included when "Allow Anonymous Connections" is also enabled, and are never affected by "Stop Skipped Channels". With "Stay on same account", several anonymous viewers behind one IP can be kept on one account; a new stream that turns out not to be a switch is moved to a free account after the window.

The change takes effect after you save the account.`}
  </div>
);

const M3U = ({
  m3uAccount = null,
  isOpen,
  onClose,
  playlistCreated = false,
}) => {
  const userAgents = useUserAgentsStore((s) => s.userAgents);
  const serverGroups = useServerGroupsStore((s) => s.serverGroups);
  const fetchChannelGroups = useChannelsStore((s) => s.fetchChannelGroups);
  const fetchEPGs = useEPGsStore((s) => s.fetchEPGs);
  const fetchCategories = useVODStore((s) => s.fetchCategories);

  const [playlist, setPlaylist] = useState(null);
  const [file, setFile] = useState(null);
  const [expDate, setExpDate] = useState(null);
  const [profileModalOpen, setProfileModalOpen] = useState(false);
  const [groupFilterModalOpen, setGroupFilterModalOpen] = useState(false);
  const [filterModalOpen, setFilterModalOpen] = useState(false);
  const [scheduleType, setScheduleType] = useState('interval');
  const [serverGroupsManagerOpen, setServerGroupsManagerOpen] = useState(false);
  const [serverGroupsCreateOnOpen, setServerGroupsCreateOnOpen] =
    useState(false);
  // Channel Switch Overlap is only enabled after its explanation is confirmed;
  // its sub-settings are only shown while it is enabled.
  const [overlapConfirmOpen, setOverlapConfirmOpen] = useState(false);
  const [overlapInfoOpen, setOverlapInfoOpen] = useState(false);
  const [overlapEnabled, setOverlapEnabled] = useState(false);

  // Keep expiration in sync when the default profile is edited (store refreshes).
  // Do not rebind the whole form to the live playlist or unsaved edits are wiped.
  const accountId = playlist?.id ?? m3uAccount?.id;
  const storeExpDate = usePlaylistsStore((s) => {
    if (!accountId) return undefined;
    const stored = s.playlists.find((p) => p.id === accountId);
    if (!stored) return undefined;
    return stored.exp_date ?? null;
  });

  const form = useForm({
    mode: 'uncontrolled',
    initialValues: {
      name: '',
      server_url: '',
      user_agent: '0',
      server_group: '0',
      is_active: true,
      max_streams: 0,
      refresh_interval: 24,
      cron_expression: '',
      account_type: 'XC',
      create_epg: false,
      username: '',
      password: '',
      stale_stream_days: 7,
      priority: 0,
      enable_vod: false,
      probation_enabled: false,
      probation_seconds: 10,
      probation_allow_anonymous: false,
      probation_stop_skipped: false,
      probation_account_preference: 'order',
    },

    validate: {
      name: isNotEmpty('Please select a name'),
      user_agent: isNotEmpty('Please select a user-agent'),
    },
  });

  useEffect(() => {
    if (m3uAccount) {
      setPlaylist(m3uAccount);
      form.setValues({
        name: m3uAccount.name,
        server_url: m3uAccount.server_url,
        max_streams: m3uAccount.max_streams,
        user_agent: m3uAccount.user_agent ? `${m3uAccount.user_agent}` : '0',
        server_group: m3uAccount.server_group
          ? `${m3uAccount.server_group}`
          : '0',
        is_active: m3uAccount.is_active,
        refresh_interval: m3uAccount.refresh_interval,
        cron_expression: m3uAccount.cron_expression || '',
        account_type: m3uAccount.account_type,
        username: m3uAccount.username ?? '',
        password: '',
        stale_stream_days:
          m3uAccount.stale_stream_days !== undefined &&
          m3uAccount.stale_stream_days !== null
            ? m3uAccount.stale_stream_days
            : 7,
        priority:
          m3uAccount.priority !== undefined && m3uAccount.priority !== null
            ? m3uAccount.priority
            : 0,
        enable_vod: m3uAccount.enable_vod || false,
        probation_enabled: m3uAccount.probation_enabled || false,
        probation_seconds: m3uAccount.probation_seconds ?? 10,
        probation_allow_anonymous:
          m3uAccount.probation_allow_anonymous || false,
        probation_stop_skipped: m3uAccount.probation_stop_skipped || false,
        probation_account_preference:
          m3uAccount.probation_account_preference || 'order',
      });
      setOverlapEnabled(m3uAccount.probation_enabled || false);
      setExpDate(expDateFromPlaylist(m3uAccount.exp_date));

      // Determine schedule type from existing data
      setScheduleType(
        m3uAccount.cron_expression && m3uAccount.cron_expression.trim() !== ''
          ? 'cron'
          : 'interval'
      );
    } else {
      setPlaylist(null);
      form.reset();
      setScheduleType('interval');
      setExpDate(null);
      setOverlapEnabled(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [m3uAccount]);

  useEffect(() => {
    if (storeExpDate === undefined) return;
    const next = expDateFromPlaylist(storeExpDate);
    setExpDate((prev) =>
      expDateKey(prev) === expDateKey(next) ? prev : next
    );
  }, [storeExpDate]);

  const handleNewPlaylist = async (newPlaylist, values, create_epg) => {
    if (create_epg) {
      addEPG({
        name: values.name,
        source_type: 'xmltv',
        url: `${new URL(values.server_url).origin}/xmltv.php?username=${values.username}&password=${values.password}`,
        api_key: '',
        is_active: true,
        refresh_interval: 24,
      });
    }

    if (values.account_type != 'XC') {
      showNotification({
        title: 'Fetching M3U Groups',
        message:
          'Configure group filters and auto sync settings once complete.',
      });
      close();
      return;
    }

    const updatedPlaylist = await getPlaylist(newPlaylist);
    await Promise.all([fetchChannelGroups(), fetchEPGs()]);

    if (values.enable_vod) {
      fetchCategories();
    }

    setPlaylist(updatedPlaylist);
    setGroupFilterModalOpen(true);
  };

  const onSubmit = async () => {
    const { create_epg, ...rawValues } = form.getValues();
    const values = prepareSubmitValues(rawValues, expDate);

    if (playlist?.id) {
      await updatePlaylist(playlist, values, file);
      form.reset();
      setOverlapEnabled(false);
      setFile(null);
      onClose();
      return;
    }

    const newPlaylist = await addPlaylist(values, file);
    await handleNewPlaylist(newPlaylist, values, create_epg);
  };

  const close = () => {
    form.reset();
    setOverlapEnabled(false);
    setFile(null);
    setPlaylist(null);
    onClose();
  };

  const closeGroupFilter = () => {
    setGroupFilterModalOpen(false);
    // After group filter setup for a new account, reset everything
    form.reset();
    setOverlapEnabled(false);
    setFile(null);
    setPlaylist(null);
    onClose();
  };

  const closeFilter = () => {
    setFilterModalOpen(false);
  };

  useEffect(() => {
    if (playlistCreated) {
      setGroupFilterModalOpen(true);
    }
  }, [playlist, playlistCreated]);

  if (!isOpen) {
    return <></>;
  }

  return (
    <>
      <Modal
        size={960}
        opened={isOpen}
        onClose={close}
        title="M3U Account"
        scrollAreaComponent={Modal.NativeScrollArea}
        lockScroll={false}
        withinPortal={true}
        trapFocus={false}
        yOffset="2vh"
      >
        <LoadingOverlay visible={form.submitting} overlayBlur={2} />

        <form onSubmit={form.onSubmit(onSubmit)}>
          <Group align="flex-start" gap="md" wrap="nowrap">
            <Stack gap="xs" style={{ flex: 1, minWidth: 0 }}>
              <TextInput
                id="name"
                name="name"
                label="Name"
                description="Unique identifier for this M3U account"
                {...form.getInputProps('name')}
                key={form.key('name')}
              />
              <TextInput
                id="server_url"
                name="server_url"
                label="URL"
                description="Direct URL to the M3U playlist or server"
                {...form.getInputProps('server_url')}
                key={form.key('server_url')}
              />
              <Select
                id="account_type"
                name="account_type"
                label="Account Type"
                description={
                  <>
                    Standard for direct M3U URLs, <br />
                    Xtream Codes for panel-based services
                  </>
                }
                data={[
                  { value: 'STD', label: 'Standard' },
                  { value: 'XC', label: 'Xtream Codes' },
                ]}
                key={form.key('account_type')}
                {...form.getInputProps('account_type')}
              />

              {form.getValues().account_type == 'XC' && (
                <>
                  <TextInput
                    id="username"
                    name="username"
                    label="Username"
                    description="Username for Xtream Codes authentication"
                    {...form.getInputProps('username')}
                  />
                  <PasswordInput
                    id="password"
                    name="password"
                    label="Password"
                    description="Password for Xtream Codes authentication (leave empty to keep existing)"
                    {...form.getInputProps('password')}
                  />
                </>
              )}

              {form.getValues().account_type != 'XC' && (
                <>
                  <FileInput
                    id="file"
                    label="Upload files"
                    placeholder="Upload files"
                    description="Upload a local M3U file instead of using URL"
                    onChange={setFile}
                    styles={{
                      input: {
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        display: 'block',
                      },
                    }}
                  />
                  <DateTimePicker
                    label="Expiration Date"
                    description="Set an expiration date to receive a warning notification"
                    placeholder="No expiration"
                    clearable
                    valueFormat="MMM D, YYYY h:mm A"
                    value={expDate}
                    onChange={(v) => setExpDate(v ? new Date(v) : null)}
                  />
                </>
              )}
            </Stack>

            <Divider size="sm" orientation="vertical" />

            <Stack gap="xs" style={{ flex: 1, minWidth: 0 }}>
              <NumberInput
                id="max_streams"
                name="max_streams"
                label="Max Streams"
                placeholder="0 = Unlimited"
                description="Maximum number of concurrent streams (0 for unlimited)"
                min={0}
                {...form.getInputProps('max_streams')}
                key={form.key('max_streams')}
              />
              <Box>
                <Switch
                  id="probation_enabled"
                  name="probation_enabled"
                  label="Allow Channel Switch Overlap"
                  description="Faster channel switching when every account is at its limit."
                  key={form.key('probation_enabled')}
                  {...form.getInputProps('probation_enabled', {
                    type: 'checkbox',
                  })}
                  onChange={(event) => {
                    if (event.currentTarget.checked) {
                      setOverlapConfirmOpen(true);
                    } else {
                      form.setFieldValue('probation_enabled', false);
                      setOverlapEnabled(false);
                    }
                  }}
                />
                <Button
                  variant="subtle"
                  size="compact-xs"
                  onClick={() => setOverlapInfoOpen(true)}
                >
                  What does this do?
                </Button>
                <Collapse in={overlapEnabled}>
                  <Stack gap="xs" pl="md" pt="xs">
                    <NumberInput
                      id="probation_seconds"
                      name="probation_seconds"
                      label="Overlap Window (seconds)"
                      description="Keep below what the provider tolerates."
                      min={1}
                      max={120}
                      {...form.getInputProps('probation_seconds')}
                      key={form.key('probation_seconds')}
                    />
                    <Switch
                      id="probation_stop_skipped"
                      name="probation_stop_skipped"
                      label="Stop Skipped Channels"
                      description="Close channels a player surfed past."
                      key={form.key('probation_stop_skipped')}
                      {...form.getInputProps('probation_stop_skipped', {
                        type: 'checkbox',
                      })}
                    />
                    <Select
                      id="probation_account_preference"
                      name="probation_account_preference"
                      label="When Switching Channels"
                      description="Which account a player's next channel prefers."
                      allowDeselect={false}
                      data={[
                        { value: 'order', label: 'Follow channel order' },
                        { value: 'same', label: 'Stay on same account' },
                        { value: 'alternate', label: 'Use another account' },
                      ]}
                      key={form.key('probation_account_preference')}
                      {...form.getInputProps('probation_account_preference')}
                    />
                    <Switch
                      id="probation_allow_anonymous"
                      name="probation_allow_anonymous"
                      label="Allow Anonymous Connections"
                      description="Match viewers without a login by IP only."
                      key={form.key('probation_allow_anonymous')}
                      {...form.getInputProps('probation_allow_anonymous', {
                        type: 'checkbox',
                      })}
                    />
                  </Stack>
                </Collapse>
              </Box>
              <Select
                id="server_group"
                name="server_group"
                label="Server Group"
                description="Share login limits across accounts in a server group. Set max streams on each profile (unlimited profiles skip group enforcement)."
                key={form.key('server_group')}
                value={form.getValues().server_group}
                onChange={(value) => {
                  if (value === '__new__') {
                    setServerGroupsCreateOnOpen(true);
                    setServerGroupsManagerOpen(true);
                    return;
                  }
                  form.setFieldValue('server_group', value);
                }}
                data={[
                  { value: '0', label: '(None)' },
                  ...serverGroups.map((group) => ({
                    label: group.name,
                    value: `${group.id}`,
                  })),
                  { value: '__new__', label: '+ Add server group...' },
                ]}
              />
              <Button
                variant="subtle"
                size="compact-xs"
                onClick={() => {
                  setServerGroupsCreateOnOpen(false);
                  setServerGroupsManagerOpen(true);
                }}
                style={{ alignSelf: 'flex-start' }}
              >
                Manage server groups
              </Button>
              <Select
                id="user_agent"
                name="user_agent"
                label="User-Agent"
                description="User-Agent header to use when accessing this M3U source"
                {...form.getInputProps('user_agent')}
                key={form.key('user_agent')}
                data={[{ value: '0', label: '(Use Default)' }].concat(
                  userAgents.map((ua) => ({
                    label: ua.name,
                    value: `${ua.id}`,
                  }))
                )}
              />
            </Stack>

            <Divider size="sm" orientation="vertical" />

            <Stack gap="xs" style={{ flex: 1, minWidth: 0 }}>
              <ScheduleInput
                scheduleType={scheduleType}
                onScheduleTypeChange={setScheduleType}
                intervalValue={form.getValues().refresh_interval}
                onIntervalChange={(v) =>
                  form.setFieldValue('refresh_interval', v)
                }
                cronValue={form.getValues().cron_expression}
                onCronChange={(expr) =>
                  form.setFieldValue('cron_expression', expr)
                }
                intervalLabel="Refresh Interval (hours)"
                intervalDescription={
                  <>
                    How often to automatically refresh M3U data
                    <br />
                    (0 to disable automatic refreshes)
                  </>
                }
              />
              <NumberInput
                min={0}
                max={365}
                label="Stale Stream Retention (days)"
                description="Streams not seen for this many days will be removed"
                {...form.getInputProps('stale_stream_days')}
              />

              {form.getValues().account_type == 'XC' && (
                <Box>
                  <NumberInput
                    min={0}
                    max={999}
                    label="VOD Priority"
                    description="Priority for VOD provider selection (higher numbers = higher priority). Used when multiple providers offer the same content."
                    {...form.getInputProps('priority')}
                    key={form.key('priority')}
                  />

                  <Group justify="space-between">
                    <Box>Enable VOD Scanning</Box>
                    <Switch
                      id="enable_vod"
                      name="enable_vod"
                      description="Scan and import VOD content (movies/series) from this Xtream account"
                      key={form.key('enable_vod')}
                      {...form.getInputProps('enable_vod', {
                        type: 'checkbox',
                      })}
                    />
                  </Group>

                  {!m3uAccount && (
                    <Group justify="space-between">
                      <Box>Create EPG</Box>
                      <Switch
                        id="create_epg"
                        name="create_epg"
                        description="Automatically create matching EPG source for this Xtream account"
                        key={form.key('create_epg')}
                        {...form.getInputProps('create_epg', {
                          type: 'checkbox',
                        })}
                      />
                    </Group>
                  )}
                </Box>
              )}
            </Stack>
          </Group>

          <Divider my="md" />

          <Flex
            gap="md"
            justify="space-between"
            align="center"
            wrap="wrap"
            mih={50}
          >
            <Switch
              id="is_active"
              name="is_active"
              label="Is Active"
              description="Enable or disable this M3U account"
              key={form.key('is_active')}
              {...form.getInputProps('is_active', { type: 'checkbox' })}
            />

            <Flex gap="xs" align="center">
              {playlist && (
                <>
                  <Button
                    variant="filled"
                    size="sm"
                    onClick={() => setFilterModalOpen(true)}
                  >
                    Filters
                  </Button>
                  <Button
                    variant="filled"
                    // color={theme.custom.colors.buttonPrimary}
                    size="sm"
                    onClick={() => {
                      // If this is an XC account with VOD enabled, fetch VOD categories
                      if (
                        m3uAccount?.account_type === 'XC' &&
                        m3uAccount?.enable_vod
                      ) {
                        fetchCategories();
                      }
                      setGroupFilterModalOpen(true);
                    }}
                  >
                    Groups
                  </Button>
                  <Button
                    variant="filled"
                    // color={theme.custom.colors.buttonPrimary}
                    size="sm"
                    onClick={() => setProfileModalOpen(true)}
                  >
                    Profiles
                  </Button>
                </>
              )}

              <Button
                type="submit"
                variant="filled"
                disabled={form.submitting}
                size="sm"
              >
                Save
              </Button>
            </Flex>
          </Flex>
        </form>
      </Modal>
      {playlist && (
        <>
          <M3UProfiles
            playlist={playlist}
            isOpen={profileModalOpen}
            onClose={() => setProfileModalOpen(false)}
            pendingExpDate={expDate}
          />
          <M3UGroupFilter
            isOpen={groupFilterModalOpen}
            playlist={playlist}
            onClose={closeGroupFilter}
          />
          <M3UFilters
            isOpen={filterModalOpen}
            playlist={playlist}
            onClose={closeFilter}
          />
        </>
      )}

      <ConfirmationDialog
        opened={overlapConfirmOpen}
        onClose={() => setOverlapConfirmOpen(false)}
        onConfirm={() => {
          form.setFieldValue('probation_enabled', true);
          setOverlapEnabled(true);
          setOverlapConfirmOpen(false);
        }}
        title="Enable Channel Switch Overlap?"
        confirmLabel="Enable"
        size="lg"
        message={OVERLAP_EXPLANATION}
      />

      <Modal
        opened={overlapInfoOpen}
        onClose={() => setOverlapInfoOpen(false)}
        title="Channel Switch Overlap"
        size="lg"
        centered
        zIndex={1000}
      >
        {OVERLAP_EXPLANATION}
        <Group justify="flex-end" mt="md">
          <Button variant="outline" onClick={() => setOverlapInfoOpen(false)}>
            Close
          </Button>
        </Group>
      </Modal>

      <ServerGroupsManagerModal
        isOpen={serverGroupsManagerOpen}
        onClose={() => {
          setServerGroupsManagerOpen(false);
          setServerGroupsCreateOnOpen(false);
        }}
        openCreateOnMount={serverGroupsCreateOnOpen}
        onGroupCreated={(group) => {
          if (group?.id) {
            form.setFieldValue('server_group', `${group.id}`);
          }
        }}
      />
    </>
  );
};

export default M3U;
