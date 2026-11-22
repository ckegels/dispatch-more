"""A recording is run by the Debian install's one worker too (dispatcharr/celery.py).

debian_install.sh starts one Celery worker without -Q; recordings are routed to the `dvr`
queue, which that worker did not listen to, so a recording never started. With the queues
declared, a worker without -Q listens to both, and one given -Q (Docker's) to what it is given.

Dispatcharr 0.34 runs plugin actions on a `plugins` queue, which its Docker workers are given
with -Q celery,plugins; the Debian install's worker listens to it through the same declaration,
also when recordings have a worker of their own.
"""

from django.test import SimpleTestCase

from dispatcharr.celery import app, default_worker_queues


class DvrQueueTests(SimpleTestCase):
    def test_a_worker_without_queues_listens_to_recordings_too(self):
        self.assertEqual({q.name for q in app.conf.task_queues}, {"celery", "dvr", "plugins"})

    def test_plugin_actions_are_heard_too(self):
        from apps.plugins.tasks import run_plugin_action_task

        queues = app.amqp.Queues(app.conf.task_queues)
        self.assertIn("plugins", queues.consume_from)
        # The action's own queue is named when it is sent (apply_async(..., queue="plugins"))
        self.assertEqual(run_plugin_action_task.name, "apps.plugins.tasks.run_plugin_action")

    def test_with_recordings_on_their_own_worker_plugins_stay(self):
        self.assertEqual(
            default_worker_queues({"DISPATCHARR_DVR_ON_DEFAULT_WORKER": "false"}),
            ("celery", "plugins"),
        )
        self.assertEqual(default_worker_queues({}), ("celery", "dvr", "plugins"))

    def test_recordings_still_go_to_their_own_queue(self):
        route = app.amqp.router.route({}, "apps.channels.tasks.run_recording")
        self.assertEqual(route["queue"].name, "dvr")

    def test_a_worker_given_a_queue_keeps_to_it(self):
        queues = app.amqp.Queues(app.conf.task_queues)
        queues.select(["celery"])
        self.assertEqual(set(queues.consume_from), {"celery"})
