"""A recording is run by the Debian install's one worker too (dispatcharr/celery.py).

debian_install.sh starts one Celery worker without -Q; recordings are routed to the `dvr`
queue, which that worker did not listen to, so a recording never started. With the queues
declared, a worker without -Q listens to both, and one given -Q (Docker's) to what it is given.
"""

from django.test import SimpleTestCase

from dispatcharr.celery import app


class DvrQueueTests(SimpleTestCase):
    def test_a_worker_without_queues_listens_to_recordings_too(self):
        self.assertEqual({q.name for q in app.conf.task_queues}, {"celery", "dvr"})

    def test_recordings_still_go_to_their_own_queue(self):
        route = app.amqp.router.route({}, "apps.channels.tasks.run_recording")
        self.assertEqual(route["queue"].name, "dvr")

    def test_a_worker_given_a_queue_keeps_to_it(self):
        queues = app.amqp.Queues(app.conf.task_queues)
        queues.select(["celery"])
        self.assertEqual(set(queues.consume_from), {"celery"})
