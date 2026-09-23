// The Channel Manager's EPG Grabber tab: iptv-org/epg, which is installed on the box,
// pointed at and run from here rather than by hand.
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import EpgGrabberTable from '../EpgGrabberTable.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getEpgGrabber: vi.fn(),
    saveEpgGrabberSettings: vi.fn(),
    runEpgGrabber: vi.fn(),
    stopEpgGrabber: vi.fn(),
    makeEpgGrabberSource: vi.fn(),
    makeEpgGrabberList: vi.fn(),
  },
}));

const job = {
  id: 'pbs',
  name: 'PBS (TV Passport)',
  enabled: true,
  channels: '/opt/iptv-org-epg/data/tvpassport-pbs.channels.xml',
  sites: '',
  days: 3,
  lang: '',
  timeout_ms: 0,
  delay_ms: 0,
  max_connections: 0,
  proxy: '',
  gzip: false,
  output: '/opt/iptv-org-epg/data/tvpassport-pbs.xmltv',
  epg_source: null,
  last: {
    ok: true, at: '2026-09-23T04:00:00+00:00', seconds: 4200, channels: 1513,
    programmes: 168231, read_by: 'PBS (TV Passport)',
  },
};

const page = {
  settings: {
    enabled: true,
    folder: '/opt/iptv-org-epg',
    command: ['npm', 'run', 'grab', '---'],
    every_hours: 12,
    window_from: '',
    window_to: '',
    silent_for_minutes: 20,
    give_up_after_minutes: 360,
    jobs: [job],
  },
  defaults: {},
  job_defaults: { id: '', name: '', enabled: true, days: 0, output: '' },
  install: { ok: true, why: '', sites: 189, runs: '/usr/bin/npm', writable: true },
  channel_files: [
    {
      path: '/opt/iptv-org-epg/data/tvpassport-pbs.channels.xml',
      site: 'data',
      channels: 1513,
    },
  ],
  running: false,
  progress: {},
  epg_sources: [{ id: 4, name: 'PBS (TV Passport)', file_path: '', url: '' }],
};

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <EpgGrabberTable />
    </MantineProvider>
  );

describe('EpgGrabberTable', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    API.getEpgGrabber.mockResolvedValue(page);
    API.saveEpgGrabberSettings.mockResolvedValue({ settings: page.settings, install: page.install });
    API.runEpgGrabber.mockResolvedValue({ started: true });
    API.stopEpgGrabber.mockResolvedValue({ stopping: true });
    API.makeEpgGrabberSource.mockResolvedValue({ id: 4, name: 'PBS (TV Passport)', made: true });
    API.makeEpgGrabberList.mockResolvedValue({
      kept: 1513, of: 20114, written: false, into: '',
      sample: ['PBS (KQED) San Francisco, CA', 'PBS12 (KBDI) Denver, CO'],
    });
  });
  afterEach(() => vi.clearAllMocks());

  it('says where the grabber is and what it found there', async () => {
    draw();
    expect(await screen.findByDisplayValue('/opt/iptv-org-epg')).toBeInTheDocument();
    expect(screen.getByText(/Found it: 189 sites, started with \/usr\/bin\/npm/)).toBeInTheDocument();
    // The command as it would have been typed, three dashes and all
    expect(screen.getByDisplayValue('npm run grab ---')).toBeInTheDocument();
  });

  it('says which part is missing when it is not there', async () => {
    API.getEpgGrabber.mockResolvedValue({
      ...page,
      install: { ok: false, why: 'It is there, but npm install has not been run in it.', sites: 0 },
    });
    draw();
    expect(
      await screen.findByText(/npm install has not been run in it/)
    ).toBeInTheDocument();
    // ...and there is nothing to grab with until it is
    expect(screen.getByRole('button', { name: /Grab now/ })).toBeDisabled();
  });

  it('shows each guide with how its last grab went', async () => {
    draw();
    expect(await screen.findByDisplayValue('PBS (TV Passport)')).toBeInTheDocument();
    expect(
      screen.getByText('168,231 programmes for 1,513 channels')
    ).toBeInTheDocument();
    expect(screen.getByText(/read by PBS \(TV Passport\)/)).toBeInTheDocument();
  });

  it('keeps what is on the page before it grabs, so what runs is what you see', async () => {
    draw();
    await screen.findByDisplayValue('PBS (TV Passport)');
    fireEvent.click(screen.getByRole('button', { name: /Grab now/ }));

    await waitFor(() => expect(API.saveEpgGrabberSettings).toHaveBeenCalled());
    expect(API.runEpgGrabber).toHaveBeenCalledWith(null);
  });

  it('grabs one guide on its own', async () => {
    draw();
    await screen.findByDisplayValue('PBS (TV Passport)');
    fireEvent.click(screen.getByRole('button', { name: /Grab this one/ }));
    await waitFor(() => expect(API.runEpgGrabber).toHaveBeenCalledWith('pbs'));
  });

  it('says how far a grab has got, and can stop it', async () => {
    API.getEpgGrabber.mockResolvedValue({
      ...page,
      running: true,
      progress: { state: 'running', name: 'PBS (TV Passport)', done: 1204, total: 4539, now: '[1204/4539] tvpassport.com' },
    });
    draw();
    expect(await screen.findByText(/1,204 of 4,539/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Stop/ }));
    await waitFor(() => expect(API.stopEpgGrabber).toHaveBeenCalled());
  });

  it('makes the EPG source that reads what it writes', async () => {
    draw();
    await screen.findByDisplayValue('PBS (TV Passport)');
    fireEvent.click(screen.getByRole('button', { name: 'Make one' }));

    await waitFor(() =>
      expect(API.makeEpgGrabberSource).toHaveBeenCalledWith(
        'PBS (TV Passport)', '/opt/iptv-org-epg/data/tvpassport-pbs.xmltv'
      )
    );
    expect(
      await screen.findByText(/Made the EPG source "PBS \(TV Passport\)"/)
    ).toBeInTheDocument();
  });

  it('adds a guide, and asks before one is removed', async () => {
    draw();
    await screen.findByDisplayValue('PBS (TV Passport)');

    fireEvent.click(screen.getByRole('button', { name: /Add a guide/ }));
    expect(await screen.findAllByDisplayValue('Guide')).toHaveLength(1);

    fireEvent.click(screen.getAllByRole('button', { name: /Remove PBS/ })[0]);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/The XMLTV file it wrote stays where it is/)).toBeInTheDocument();
  });

  it('makes a channel list out of a bigger one, showing it before writing it', async () => {
    // The grep that was being done by hand: keep the ones saying PBS, see what that is,
    // and only then write the list
    draw();
    await screen.findByDisplayValue('PBS (TV Passport)');

    // From the dropdown that belongs to this field: the same path is the value of a
    // guide's own Channel list above
    const field = screen.getByRole('textbox', { name: 'Out of' });
    fireEvent.click(field);
    const dropdown = document.getElementById(field.getAttribute('aria-controls'));
    fireEvent.click(await within(dropdown).findByText(/tvpassport-pbs.channels.xml/));
    fireEvent.change(screen.getByLabelText('Keep the ones saying'), {
      target: { value: 'PBS' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Show me' }));

    expect(await screen.findByText(/1,513 of 20,114 channels/)).toBeInTheDocument();
    expect(screen.getByText(/PBS12 \(KBDI\) Denver, CO/)).toBeInTheDocument();
    // Nothing is written until it is asked for
    expect(API.makeEpgGrabberList).toHaveBeenLastCalledWith(
      expect.objectContaining({ keep: 'PBS', apply: false })
    );

    API.makeEpgGrabberList.mockResolvedValue({
      kept: 1513, of: 20114, written: true,
      into: '/opt/iptv-org-epg/data/pbs.channels.xml', sample: [],
    });
    fireEvent.click(screen.getByRole('button', { name: 'Make it' }));

    await waitFor(() =>
      expect(API.makeEpgGrabberList).toHaveBeenLastCalledWith(
        expect.objectContaining({ apply: true })
      )
    );
    expect(
      await screen.findByText(/Made \/opt\/iptv-org-epg\/data\/pbs.channels.xml/)
    ).toBeInTheDocument();
  });

  it('says what went wrong rather than nothing', async () => {
    API.runEpgGrabber.mockRejectedValue({ body: { error: 'A grab is already running' } });
    draw();
    await screen.findByDisplayValue('PBS (TV Passport)');
    fireEvent.click(screen.getByRole('button', { name: /Grab now/ }));
    expect(await screen.findByText('A grab is already running')).toBeInTheDocument();
  });
});
