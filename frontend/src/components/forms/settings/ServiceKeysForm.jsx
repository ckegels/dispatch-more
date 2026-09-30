import React, { useEffect, useState } from 'react';
import { ExternalLink } from 'lucide-react';
import {
  Alert,
  Anchor,
  Button,
  Group,
  Paper,
  PasswordInput,
  Stack,
  Text,
} from '@mantine/core';
import API from '../../../api';

// The keys for online services, in one place so every feature that asks one of them uses
// the same key. Show Groups is the first: it asks them what a show is.
const ServiceKeysForm = () => {
  const [services, setServices] = useState([]);
  const [saved, setSaved] = useState({});
  const [values, setValues] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState('');
  const [tested, setTested] = useState({});

  useEffect(() => {
    API.getServiceKeys()
      .then((answer) => {
        setServices(answer.services);
        setSaved(answer.values);
        setValues(answer.values);
      })
      .catch((e) => setError(e?.body?.error || 'The keys could not be read.'));
  }, []);

  const changed = (service) =>
    service.fields.some(
      (field) => (values[field.key] || '') !== (saved[field.key] || '')
    );

  const save = async (service) => {
    setBusy(service.id);
    setError(null);
    try {
      const answer = await API.saveServiceKeys(
        Object.fromEntries(
          service.fields.map((field) => [field.key, values[field.key] || ''])
        )
      );
      setSaved(answer.values);
      setValues((before) => ({ ...before, ...answer.values }));
      setTested((before) => ({ ...before, [service.id]: null }));
    } catch (e) {
      setError(e?.body?.error || 'The key was not saved.');
    } finally {
      setBusy('');
    }
  };

  const test = async (service) => {
    setBusy(service.id);
    try {
      const answer = await API.testServiceKey(service.id);
      setTested((before) => ({ ...before, [service.id]: answer }));
    } catch (e) {
      setTested((before) => ({
        ...before,
        [service.id]: {
          ok: false,
          message: e?.body?.error || 'It could not be tested.',
        },
      }));
    } finally {
      setBusy('');
    }
  };

  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">
        Keys for online services. Everything in Dispatch More that asks one of
        them uses the key here; a service without a key is not asked. Show
        Groups asks them what a show is.
      </Text>
      {error && <Alert color="red">{error}</Alert>}
      {services.map((service) => (
        <Paper key={service.id} withBorder p="md">
          <Group justify="space-between" align="flex-start" mb="xs">
            <div>
              <Text fw={600}>{service.name}</Text>
              <Text size="xs" c="dimmed">
                {service.about}
              </Text>
            </div>
            <Anchor
              href={service.url}
              target="_blank"
              rel="noreferrer"
              size="xs"
            >
              <Group gap={4}>
                Get a key <ExternalLink size={12} />
              </Group>
            </Anchor>
          </Group>
          <Stack gap="xs">
            {service.fields.map((field) => (
              <PasswordInput
                key={field.key}
                size="xs"
                label={field.label}
                value={values[field.key] || ''}
                onChange={(event) => {
                  const value = event.currentTarget.value;
                  setValues((before) => ({ ...before, [field.key]: value }));
                }}
              />
            ))}
            <Group gap="sm">
              <Button
                size="xs"
                onClick={() => save(service)}
                disabled={!changed(service) || !!busy}
                loading={busy === service.id && changed(service)}
              >
                Save
              </Button>
              <Button
                size="xs"
                variant="default"
                onClick={() => test(service)}
                disabled={
                  !saved[service.fields[0].key] || changed(service) || !!busy
                }
                loading={busy === service.id && !changed(service)}
              >
                Test
              </Button>
              {tested[service.id] && (
                <Text size="xs" c={tested[service.id].ok ? 'green.4' : 'red.4'}>
                  {tested[service.id].message}
                </Text>
              )}
            </Group>
          </Stack>
        </Paper>
      ))}
    </Stack>
  );
};

export default ServiceKeysForm;
