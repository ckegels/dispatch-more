"""Translated captions (captions/translate.py, fork/subtitles.md §9 step 4): one translation per
(channel, language) shared by every TV, lines in order, the engine chosen, and the worker's
Opus-MT pairs (through English when there is no direct one)."""

from unittest import mock

from django.test import SimpleTestCase

from apps.channels.captions import translate, worker


class FakeRedis:
    def __init__(self):
        self.data = {}

    def hgetall(self, key):
        return dict(self.data.get(key) or {})

    def hset(self, key, mapping):
        self.data.setdefault(key, {}).update(mapping)

    def expire(self, key, seconds):
        pass

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def delete(self, key):
        self.data.pop(key, None)


def answer(language="nl", seqs=(1, 2)):
    return {"state": "listening", "language": language,
            "cues": [{"seq": s, "start": 10.0 + s, "end": 11.0 + s, "text": f"regel {s}"} for s in seqs]}


class TranslatedTests(SimpleTestCase):
    def setUp(self):
        self.redis = FakeRedis()

    def run_with(self, result, **kw):
        return translate.translated(result, "chan", kw.pop("lang", "en"), settings={}, redis_client=self.redis, **kw)

    def test_same_seq_and_times_in_the_language_asked(self):
        with mock.patch.object(translate, "engine", return_value=("deepl", "key")), \
             mock.patch.object(translate, "run", return_value=["line 1", "line 2"]) as run:
            out = self.run_with(answer())
        self.assertEqual([(c["seq"], c["start"], c["text"], c["original"]) for c in out["cues"]],
                         [(1, 11.0, "line 1", "regel 1"), (2, 12.0, "line 2", "regel 2")])
        run.assert_called_once_with("deepl", "key", {}, ["regel 1", "regel 2"], "nl", "en")
        self.assertEqual(out["translation"]["state"], "translated")

    def test_translated_once_for_every_tv(self):
        with mock.patch.object(translate, "engine", return_value=("deepl", "key")), \
             mock.patch.object(translate, "run", return_value=["line 1", "line 2"]) as run:
            self.run_with(answer())
            again = self.run_with(answer())
        self.assertEqual(run.call_count, 1)
        self.assertEqual([c["text"] for c in again["cues"]], ["line 1", "line 2"])

    def test_a_line_not_yet_translated_holds_back_the_ones_after_it(self):
        self.redis.hset(translate.cache_key("chan", "en"), {"1": "line 1", "3": "line 3"})
        self.redis.set(translate.cache_key("chan", "en") + ":lock", "1")  # another TV is on it
        with mock.patch.object(translate, "engine", return_value=("deepl", "key")):
            out = self.run_with(answer(seqs=(1, 2, 3)))
        self.assertEqual([c["seq"] for c in out["cues"]], [1])

    def test_same_language_and_no_language_yet(self):
        same = self.run_with(answer(language="en"))
        self.assertEqual([c["text"] for c in same["cues"]], ["regel 1", "regel 2"])
        waiting = self.run_with(answer(language=""))
        self.assertEqual(waiting["cues"], [])

    def test_no_translator_or_a_failure_gives_the_original_lines(self):
        with mock.patch.object(translate, "engine", return_value=(None, "No translator")):
            out = self.run_with(answer())
        self.assertEqual((out["translation"]["state"], len(out["cues"])), ("not available", 2))
        with mock.patch.object(translate, "engine", return_value=("ollama", "qwen")), \
             mock.patch.object(translate, "run", side_effect=ValueError("no answer")):
            out = self.run_with(answer())
        self.assertEqual((out["translation"]["state"], out["cues"][0]["text"]), ("failed", "regel 1"))

    def test_without_lang_nothing_changes(self):
        original = answer()
        self.assertIs(translate.translated(original, "chan", "", settings={}, redis_client=self.redis), original)


class EngineTests(SimpleTestCase):
    def test_deepl_then_ollama_then_the_worker(self):
        with mock.patch("apps.channels.service_keys.load", return_value={"deepl_key": "k:fx"}):
            self.assertEqual(translate.engine({}), ("deepl", "k:fx"))
        with mock.patch("apps.channels.service_keys.load", return_value={}), \
             mock.patch.object(translate.manager, "ollama", return_value=["qwen2.5:7b"]):
            self.assertEqual(translate.engine({}), ("ollama", "qwen2.5:7b"))
        with mock.patch("apps.channels.service_keys.load", return_value={}), \
             mock.patch.object(translate.manager, "ollama", return_value=None), \
             mock.patch.object(translate.manager, "ask", return_value={"translate": True}):
            self.assertEqual(translate.engine({}), ("opus-mt", ""))
        self.assertIsNone(translate.engine({"translator": "off"})[0])

    def test_ollama_answer_one_line_per_line(self):
        class Answer:
            def __init__(self, text):
                self.text = text

            def read(self):
                return self.text

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        good = Answer(b'{"response": "{\\"lines\\": [\\"Good evening\\", \\"Welcome\\"]}"}')
        with mock.patch("urllib.request.urlopen", return_value=good):
            self.assertEqual(translate.ollama("http://o", "m", ["Goedenavond", "Welkom"], "nl", "en"),
                             ["Good evening", "Welcome"])
        short = Answer(b'{"response": "[\\"Good evening\\"]"}')
        with mock.patch("urllib.request.urlopen", return_value=short), self.assertRaises(ValueError):
            translate.ollama("http://o", "m", ["Goedenavond", "Welkom"], "nl", "en")


class OpusPairTests(SimpleTestCase):
    def test_direct_pair_else_through_english(self):
        calls = []

        def pair(models_dir, name):
            calls.append(name)
            return None if name == "nl-de" else name

        with mock.patch.object(worker, "opus_pair", side_effect=pair), \
             mock.patch.object(worker, "_run_pair", side_effect=lambda found, lines: [f"{found}:{l}" for l in lines]):
            out = worker.translate_lines("/m", ["hallo"], "nl", "de")
            self.assertEqual(out, {"lines": ["en-de:nl-en:hallo"], "via": ["nl-en", "en-de"]})
            self.assertEqual(worker.translate_lines("/m", ["hallo"], "nl", "en")["via"], ["nl-en"])
            self.assertEqual(worker.translate_lines("/m", ["hallo"], "nl", "nl"), {"lines": ["hallo"], "via": []})
        self.assertEqual(calls[:3], ["nl-de", "nl-en", "en-de"])
