"""Each plugin logs under its own name (apps.plugins.loader._build_context).

Every plugin was handed the loader's logger, so every line a plugin wrote said
"apps.plugins.loader" and never which plugin -- with a dozen installed, the one being
looked into cannot be found. The name is all that changed: the handlers and the level are
the root's, which is where the loader's lines went too. See core.log_center, which is what
reads them back.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from apps.plugins.loader import LoadedPlugin, PluginConfig, PluginManager


class PluginLoggerTests(SimpleTestCase):
    def _context(self, key):
        with patch.object(PluginManager, "__init__", lambda self: None):
            manager = PluginManager()
        return manager._build_context(
            LoadedPlugin(key=key, name=key.title()), PluginConfig(key=key, name=key.title())
        )

    def test_a_plugin_is_given_a_logger_named_after_it(self):
        self.assertEqual(self._context("recipes")["logger"].name, "plugins.recipes")

    def test_and_two_plugins_do_not_share_one(self):
        self.assertNotEqual(
            self._context("recipes")["logger"].name,
            self._context("tuner-tools")["logger"].name,
        )

    def test_what_it_writes_says_which_plugin_wrote_it(self):
        logger = self._context("recipes")["logger"]
        with self.assertLogs("plugins.recipes", level="INFO") as said:
            logger.info("Cooked 3 channels")
        self.assertEqual(said.records[0].name, "plugins.recipes")
