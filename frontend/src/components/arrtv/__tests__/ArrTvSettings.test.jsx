// With the real Mantine: the switches, their order of dependence, and the reports under them
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import ArrTvSettings from '../ArrTvSettings.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getArrTvSettings: vi.fn(),
    saveArrTvSettings: vi.fn(),
    getAppReports: vi.fn(),
    getAppReport: vi.fn(),
    deleteAppReport: vi.fn(),
    getArrTvHeld: vi.fn(),
    forgetArrTvHeld: vi.fn(),
    getArrTvGuideChanges: vi.fn(),
    putBackArrTvGuide: vi.fn(),
    keepArrTvGuide: vi.fn(),
    getArrTvGuidePreload: vi.fn(),
    startArrTvGuidePreload: vi.fn(),
  },
}));

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <ArrTvSettings />
    </MantineProvider>
  );

// Mantine's Select scrolls to the chosen option, which jsdom does not have
Element.prototype.scrollIntoView = vi.fn();

describe('ArrTvSettings', () => {
  afterEach(() => vi.clearAllMocks());

  it('is all off at first, and the channel change needs the devices', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: false,
      switch_hints: false,
      reports: false,
    });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.saveArrTvSettings.mockResolvedValue({
      devices: true,
      switch_hints: false,
      reports: false,
    });
    draw();

    // In the order they are shown: devices, the channel change under it, reports
    await screen.findByText('Recognise each arrTV device');
    const [devices, hints] = screen.getAllByRole('switch');
    expect(devices).not.toBeChecked();
    expect(hints).toBeDisabled();
    // Every switch under it (channel change, stutter, faster failover, alternatives) says what it needs
    expect(
      screen.getAllByText(/Needs "Recognise each arrTV device"/)
    ).toHaveLength(4);

    fireEvent.click(devices);
    await waitFor(() =>
      expect(API.saveArrTvSettings).toHaveBeenCalledWith({
        devices: true,
        switch_hints: false,
        reports: false,
      })
    );
    await waitFor(() => expect(hints).not.toBeDisabled());
  });

  it('changes stream on stutter only with devices, and lets a held device go', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: true,
      switch_hints: false,
      reports: false,
      stall_switch: true,
    });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.getArrTvHeld.mockResolvedValue({
      held: [
        {
          device: 'app|1|shield-0001',
          name: 'admin · Living room SHIELD',
          where: 'away',
          quality: 'HD',
          until: null,
        },
      ],
    });
    API.forgetArrTvHeld.mockResolvedValue({ held: [] });
    draw();

    const stutter = await screen.findByRole('switch', {
      name: /Change stream when arrTV stutters/,
    });
    expect(stutter).toBeChecked();
    expect(stutter).not.toBeDisabled();
    expect(
      await screen.findByText(
        'admin · Living room SHIELD: starts within HD away from home'
      )
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Forget' }));
    await waitFor(() =>
      expect(API.forgetArrTvHeld).toHaveBeenCalledWith(
        'app|1|shield-0001',
        'away'
      )
    );
    await waitFor(() =>
      expect(screen.queryByText(/starts within HD/)).not.toBeInTheDocument()
    );
  });

  it('asks nothing about held devices while the stutter switch is off', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: false,
      switch_hints: false,
      reports: false,
      stall_switch: false,
    });
    API.getAppReports.mockResolvedValue({ reports: [] });
    draw();
    const stutter = await screen.findByRole('switch', {
      name: /Change stream when arrTV stutters/,
    });
    expect(stutter).toBeDisabled();
    expect(API.getArrTvHeld).not.toHaveBeenCalled();
  });

  it('switches a stream of its own on', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: true,
      switch_hints: false,
      reports: false,
      own_stream: false,
    });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.saveArrTvSettings.mockResolvedValue({
      devices: true,
      switch_hints: false,
      reports: false,
      own_stream: true,
    });
    draw();
    const own = await screen.findByRole('switch', {
      name: /A stream of its own/,
    });
    expect(own).not.toBeChecked();
    fireEvent.click(own);
    await waitFor(() =>
      expect(API.saveArrTvSettings).toHaveBeenCalledWith(
        expect.objectContaining({ own_stream: true })
      )
    );
    await waitFor(() => expect(own).toBeChecked());
  });

  it('offers every quality away from home, FHD included', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: false,
      switch_hints: false,
      reports: false,
      home_networks: '',
      outside_max_quality: 'FHD',
    });
    API.getAppReports.mockResolvedValue({ reports: [] });
    draw();
    const select = await screen.findByRole('textbox', {
      name: 'Away from home, at most',
    });
    expect(select).toHaveValue('FHD (1080p)');
    fireEvent.click(select);
    // The dropdown is still in its opening transition, which hides it from roles
    const options = await screen.findAllByRole('option', { hidden: true });
    expect(options.map((o) => o.textContent)).toEqual([
      'No limit (4K)',
      'FHD (1080p)',
      'HD (720p)',
      'SD (576p)',
    ]);
  });

  it('saves the home networks when the field is left, and says when one is not a network', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: false,
      switch_hints: false,
      reports: false,
      home_networks: '',
      outside_max_quality: 'HD',
    });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.saveArrTvSettings.mockRejectedValueOnce({
      body: { error: 'nope is not a valid IP address or subnet' },
    });
    draw();
    const field = await screen.findByPlaceholderText('192.168.2.0/24');
    fireEvent.change(field, { target: { value: 'nope' } });
    expect(API.saveArrTvSettings).not.toHaveBeenCalled();
    fireEvent.blur(field);
    await waitFor(() =>
      expect(API.saveArrTvSettings).toHaveBeenCalledWith(
        expect.objectContaining({ home_networks: 'nope' })
      )
    );
    expect(
      await screen.findByText('nope is not a valid IP address or subnet')
    ).toBeInTheDocument();
  });

  it('lists the reports arrTV sent and opens one whole', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: true,
      switch_hints: false,
      reports: true,
    });
    API.getAppReports.mockResolvedValue({
      reports: [
        {
          id: 'r1',
          received_at: 1790000000,
          user: 'tv',
          device_name: 'Living room SHIELD',
          what: 'Picture froze',
          channel: '┃AT┃ ORF 1',
          error: 'HttpDataSourceException 503',
        },
      ],
    });
    API.getAppReport.mockResolvedValue({
      id: 'r1',
      what: 'Picture froze',
      app: { name: 'arrTV' },
      player: { state: 'BUFFERING' },
      server: {
        channel: { name: '┃AT┃ ORF 1', streams: [] },
        log: ['a server line'],
      },
      log: 'an app line',
    });
    draw();

    expect(
      await screen.findByText('┃AT┃ ORF 1 — Picture froze')
    ).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole('button', { name: /Problem reports/ })
    );
    fireEvent.click(
      await screen.findByRole('button', { name: 'Open report r1' })
    );
    expect(await screen.findByText('a server line')).toBeInTheDocument();
    expect(screen.getByText('an app line')).toBeInTheDocument();
  });

  it('asks before deleting every report, since they are kept until deleted', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: true,
      switch_hints: false,
      reports: true,
    });
    API.getAppReports.mockResolvedValue({
      reports: [
        {
          id: 'r1',
          received_at: 1790000000,
          what: 'Picture froze',
          channel: 'A',
          error: '',
        },
        {
          id: 'r2',
          received_at: 1790000100,
          what: 'No sound',
          channel: 'B',
          error: '',
        },
      ],
    });
    API.deleteAppReport.mockResolvedValue({ deleted: 'all' });
    draw();

    fireEvent.click(
      await screen.findByRole('button', { name: /Problem reports/ })
    );
    fireEvent.click(
      await screen.findByRole('button', { name: 'Delete all reports' })
    );
    expect(API.deleteAppReport).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/All 2 reports are deleted/)
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Delete all' }));
    await waitFor(() => expect(API.deleteAppReport).toHaveBeenCalledWith(null));
  });

  it('lets arrTV change a guide, shows what is loaded, and puts a change back', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: false,
      reports: false,
      guide_choice: true,
    });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.getArrTvGuidePreload.mockResolvedValue({
      state: 'done',
      kept: 4812,
      preloaded_at: '2026-09-27T06:10:00+00:00',
      done: 0,
      total: 0,
    });
    API.startArrTvGuidePreload.mockResolvedValue({
      state: 'working',
      stage: "finding each channel's guides",
      done: 25,
      total: 1360,
      kept: 0,
    });
    API.getArrTvGuideChanges.mockResolvedValue({
      changes: [
        {
          channel: 7,
          channel_name: '┃AT┃ ORF 1',
          guide_name: 'ORF 1 HD',
          was_name: 'ORF1.at',
          at: '2026-09-27T17:41:02+00:00',
          by: {
            via: 'arrTV',
            username: 'alice',
            device_name: 'Living room SHIELD',
            ip: '192.168.2.40',
          },
        },
      ],
    });
    API.putBackArrTvGuide.mockResolvedValue({ changes: [] });
    draw();

    expect(
      await screen.findByText("Let arrTV change a channel's guide")
    ).toBeInTheDocument();
    // It does not need the devices recognised: a device that does not say who it is can still use it
    expect(
      screen.getByRole('switch', { name: /Let arrTV change a channel's guide/ })
    ).not.toBeDisabled();
    expect(
      await screen.findByText(/Programmes loaded for 4812 guides/)
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load now' }));
    expect(await screen.findByText(/25 of 1360 channels/)).toBeInTheDocument();

    expect(
      await screen.findByText(
        /by alice on Living room SHIELD \(192\.168\.2\.40\)/
      )
    ).toBeInTheDocument();
    expect(screen.getByText('Before: ORF1.at')).toBeInTheDocument();
    expect(screen.getByText('Now on: ORF 1 HD')).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole('button', { name: /Guide changes made in arrTV/ })
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Put back' }));
    await waitFor(() => expect(API.putBackArrTvGuide).toHaveBeenCalledWith(7));
    expect(
      await screen.findByText(/No channel has been put on another guide/)
    ).toBeInTheDocument();
  });

  it('keeps a change, says who confirmed it, and offers no put back once changed elsewhere', async () => {
    API.getArrTvSettings.mockResolvedValue({ guide_choice: true });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.getArrTvGuidePreload.mockResolvedValue({
      state: 'done',
      kept: 0,
      done: 0,
      total: 0,
    });
    API.getArrTvGuideChanges.mockResolvedValue({
      changes: [
        {
          channel: 7,
          channel_name: '┃AT┃ ORF 1',
          guide_name: 'ORF 1 HD',
          was_name: 'ORF1.at',
          at: '2026-09-27T17:41:02+00:00',
          in_force: true,
          earlier: 1,
          by: { via: 'arrTV', username: 'alice', device_name: 'SHIELD' },
          confirmed: [
            {
              username: 'bob',
              device_name: 'Chromecast',
              at: '2026-09-27T18:00:00+00:00',
            },
          ],
        },
        {
          channel: 8,
          channel_name: '┃DE┃ ZDF',
          guide_name: 'ZDF.de',
          was_name: '',
          at: '2026-09-27T17:00:00+00:00',
          in_force: false,
          earlier: 0,
          by: { via: 'arrTV', username: 'alice' },
        },
      ],
    });
    API.keepArrTvGuide.mockResolvedValue({ changes: [] });
    draw();

    expect(
      await screen.findByText(/Confirmed as right by bob on Chromecast/)
    ).toBeInTheDocument();
    expect(screen.getByText(/1 earlier change in arrTV/)).toBeInTheDocument();
    expect(
      screen.getByText(/changed again since, outside arrTV/)
    ).toBeInTheDocument();
    // One Put back: the change made elsewhere since is only taken off the list
    fireEvent.click(
      await screen.findByRole('button', { name: /Guide changes made in arrTV/ })
    );
    expect(
      await screen.findAllByRole('button', { name: 'Put back' })
    ).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Keep' }));
    await waitFor(() => expect(API.keepArrTvGuide).toHaveBeenCalledWith(7));
    expect(
      await screen.findByText(/No channel has been put on another guide/)
    ).toBeInTheDocument();
  });

  it('shows what each guide has on now, says an empty one is empty, and plays the channel', async () => {
    const useVideoStore = (await import('../../../store/useVideoStore'))
      .default;
    const showVideo = vi.fn();
    useVideoStore.setState({ showVideo });
    API.getArrTvSettings.mockResolvedValue({ guide_choice: true });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.getArrTvGuidePreload.mockResolvedValue({
      state: 'done',
      kept: 0,
      done: 0,
      total: 0,
    });
    API.getArrTvGuideChanges.mockResolvedValue({
      changes: [
        {
          channel: 9,
          channel_uuid: 'u-9',
          channel_name: 'PBS 11 | TALLAHASSEE | WFSU',
          epg: 5,
          guide_name: 'PBS (WFSU) Tallahassee, FL',
          guide_holds: 0,
          guide_now: null,
          was: 4,
          was_name: 'PBS (WFSU) Tallahassee, FL HD.us',
          was_holds: 30,
          was_now: {
            title: 'Antiques Roadshow',
            start: '2026-09-28T20:00:00Z',
            end: '2026-09-28T21:00:00Z',
          },
          at: '2026-09-28T20:33:50+00:00',
          in_force: true,
          earlier: 0,
          by: { via: 'arrTV', username: 'admin' },
        },
      ],
    });
    draw();

    fireEvent.click(
      await screen.findByRole('button', { name: /Guide changes made in arrTV/ })
    );
    expect(
      await screen.findByText(/Tallahassee, FL — holds no programmes/)
    ).toBeInTheDocument();
    expect(screen.getByText(/Antiques Roadshow/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Watch' }));
    expect(showVideo).toHaveBeenCalledWith(
      expect.stringContaining('/proxy/ts/stream/u-9'),
      'live',
      { name: 'PBS 11 | TALLAHASSEE | WFSU', channelId: 9 }
    );
  });

  it('says a preload stopped part way, and lets it be started again', async () => {
    API.getArrTvSettings.mockResolvedValue({ guide_choice: true });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.getArrTvGuideChanges.mockResolvedValue({ changes: [] });
    API.getArrTvGuidePreload.mockResolvedValue({
      state: 'stopped',
      kept: 12,
      done: 0,
      total: 0,
    });
    draw();

    expect(
      await screen.findByText(/stopped before it finished/)
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Load now' })).not.toBeDisabled();
  });

  it('says so when a put back is refused, rather than looking as though it worked', async () => {
    API.getArrTvSettings.mockResolvedValue({ guide_choice: true });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.getArrTvGuidePreload.mockResolvedValue({
      state: 'done',
      kept: 0,
      done: 0,
      total: 0,
    });
    const change = {
      channel: 7,
      channel_name: '┃AT┃ ORF 1',
      guide_name: 'ORF 1 HD',
      was_name: 'ORF1.at',
      at: '2026-09-27T17:41:02+00:00',
      in_force: true,
      by: { via: 'arrTV', username: 'alice' },
    };
    API.getArrTvGuideChanges.mockResolvedValue({ changes: [change] });
    API.putBackArrTvGuide.mockRejectedValue({
      body: { error: "The channel's guide was changed again since" },
    });
    draw();

    fireEvent.click(
      await screen.findByRole('button', { name: /Guide changes made in arrTV/ })
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Put back' }));
    expect(await screen.findByText(/changed again since/)).toBeInTheDocument();
  });

  it('saves the sources to offer as a list of ids', async () => {
    API.getArrTvSettings.mockResolvedValue({ guide_choice: false });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.saveArrTvSettings.mockResolvedValue({
      guide_choice: true,
      guide_choice_sources: '',
    });
    API.getArrTvGuidePreload.mockResolvedValue({
      state: 'working',
      stage: '',
      done: 0,
      total: 0,
      kept: 0,
    });
    API.getArrTvGuideChanges.mockResolvedValue({ changes: [] });
    draw();

    const guideSwitch = await screen.findByRole('switch', {
      name: /Let arrTV change a channel's guide/,
    });
    // Off, none of its parts are there
    expect(screen.queryByText('Sources to offer')).toBeNull();
    fireEvent.click(guideSwitch);
    await waitFor(() =>
      expect(API.saveArrTvSettings).toHaveBeenCalledWith(
        expect.objectContaining({ guide_choice: true })
      )
    );
    expect(await screen.findByText('Sources to offer')).toBeInTheDocument();
  });
});
