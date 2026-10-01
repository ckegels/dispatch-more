// The Subtitles tab's caption worker card: closed until opened, what the server has and what
// fits it, installing on Linux, the container in Docker, and measuring models.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import CaptionsCard, { machineLine } from '../CaptionsCard.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getCaptions: vi.fn(),
    setCaptions: vi.fn(),
    captionsAction: vi.fn(),
  },
}));

const machine = {
  gpus: [{ name: 'RTX 3080', memory_mb: 10240 }],
  cpu: { cores: 16, arch: 'x86_64', avx2: true },
  memory: { total_mb: 32000, free_mb: 8000 },
};
const model = (name, extra = {}) => ({
  name,
  label: `Whisper ${name}`,
  download_mb: 100,
  downloaded: false,
  ...extra,
});
const status = (over = {}) => ({
  settings: { channels_at_once: 2, quality: 'balanced', worker_url: '', token: '' },
  layout: 'systemd',
  worker_url: 'http://127.0.0.1:9725',
  worker: { running: false, error: '', device: '', cuda_failed: '' },
  install: { can_request: true, requested: false },
  docker: null,
  machine: { ...machine, seen_from: 'dispatcharr' },
  models: [model('tiny'), model('small')],
  proposal: {
    model: 'large-v3-turbo',
    translation: 'ollama',
    measured: false,
    why: 'An NVIDIA card with 10 GB: the best fast model.',
  },
  translation: { ollama: null, deepl: false },
  ...over,
});

const open = async () => {
  render(
    <MantineProvider theme={theme}>
      <CaptionsCard />
    </MantineProvider>
  );
  expect(API.getCaptions).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Captions from the sound'));
  await waitFor(() => expect(API.getCaptions).toHaveBeenCalled());
};

describe('CaptionsCard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('describes the machine in one line', () => {
    expect(machineLine(machine)).toBe(
      'RTX 3080 (10.0 GB) · 16 processor threads with AVX2 · 31.3 GB memory, 7.8 GB free'
    );
    expect(machineLine({ gpus: [], cpu: { cores: 4, arch: 'aarch64' } })).toBe(
      'No NVIDIA card · 4 processor threads (aarch64)'
    );
  });

  it('shows what fits before anything is installed, and asks for the install', async () => {
    API.getCaptions.mockResolvedValue(status());
    API.captionsAction.mockResolvedValue(
      status({ install: { can_request: true, requested: true } })
    );
    await open();
    expect(await screen.findByText(/Fits this server: large-v3-turbo \(a guess until measured\)/)).toBeInTheDocument();
    expect(screen.getByText(/no Ollama found/)).toBeInTheDocument();
    fireEvent.click(screen.getByText('Install the caption worker'));
    await waitFor(() => expect(API.captionsAction).toHaveBeenCalledWith('install', undefined));
    expect(await screen.findByText(/installer starts within a few seconds/)).toBeInTheDocument();
  });

  it('shows what the installer is doing, not that it was asked', async () => {
    API.getCaptions.mockResolvedValue(
      status({
        install: {
          can_request: true,
          requested: true,
          state: 'installing',
          step: "Installing NVIDIA's CUDA libraries for the card (about 1.5 GB)",
        },
      })
    );
    await open();
    expect(await screen.findByText(/Installing NVIDIA's CUDA libraries/)).toBeInTheDocument();
    expect(screen.queryByText(/installer starts within/)).not.toBeInTheDocument();
  });

  it('gives Docker a container to add instead of an install button', async () => {
    API.getCaptions.mockResolvedValue(
      status({
        layout: 'docker',
        install: { can_request: false },
        docker: { cpu: '  dispatch-more-captions:\n    image: python', gpu: '  dispatch-more-captions:\n    deploy:' },
      })
    );
    await open();
    expect(await screen.findByText(/a container of its own/)).toBeInTheDocument();
    expect(screen.queryByText('Install the caption worker')).not.toBeInTheDocument();
    expect(screen.getByText(/With an NVIDIA card/)).toBeInTheDocument();
  });

  it('downloads and measures models once the worker runs', async () => {
    API.getCaptions.mockResolvedValue(
      status({
        worker: { running: true, device: 'cuda' },
        models: [
          model('tiny', {
            downloaded: true,
            benchmark: { channels: 17, rtf: 0.034, device: 'cuda' },
          }),
          model('small'),
        ],
        proposal: { model: 'tiny', measured: true, why: 'Measured.', translation: '' },
      })
    );
    API.captionsAction.mockResolvedValue(status({ worker: { running: true } }));
    await open();
    expect(await screen.findByText(/17 channels at once \(0.03 s per second of sound, card\)/)).toBeInTheDocument();
    expect(screen.getByText('Worker running on the card')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() => expect(API.captionsAction).toHaveBeenCalledWith('download', 'small'));
  });
});
