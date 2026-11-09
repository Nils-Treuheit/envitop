"""Smoke tests that render the Textual app headlessly with demo data."""

from __future__ import annotations

import unittest

from omnitop.app import OmniTop
from omnitop.draw import human_bytes, human_rate, human_tokens
from omnitop.widgets import AgentTable, GpuPanel


class DemoAppTest(unittest.IsolatedAsyncioTestCase):
    async def test_wide_layout_and_modal_keys(self) -> None:
        app = OmniTop(scope="all", theme="nord", demo=True)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause()
            self.assertFalse(app.narrow)
            self.assertTrue(app.query_one("#agents", AgentTable).sessions)
            self.assertIn("GPU0", app.query_one("#gpu-panel", GpuPanel).render().plain)

            await pilot.press("?")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 2)
            self.assertEqual(type(app.screen).__name__, "HelpScreen")

            # Modal screens own the keyboard: this must close help and must
            # not open a kill confirmation behind it.
            await pilot.press("x")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertIsNone(app.confirm_kill)

            # Demo mode never touches real PIDs.
            await pilot.press("x")
            await pilot.pause()
            self.assertIsNone(app.confirm_kill)
            self.assertIn("Demo mode", app.status_message)

            # With demo protection off, the normal confirmation flow still works.
            app.demo = False
            await pilot.press("x")
            await pilot.pause()
            self.assertIsNotNone(app.confirm_kill)
            await pilot.press("n")
            await pilot.pause()
            self.assertIsNone(app.confirm_kill)

    async def test_narrow_view_switching(self) -> None:
        app = OmniTop(scope="all", demo=True)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause()
            await pilot.resize_terminal(100, 40)
            await pilot.pause()
            self.assertTrue(app.narrow)
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.view, "gpu")
            self.assertEqual(app.focus_area, "gpuprocs")
            await pilot.press("3")
            await pilot.pause()
            self.assertEqual(app.view, "agents")
            self.assertEqual(app.focus_area, "agents")


class DrawHelpersTest(unittest.TestCase):
    def test_human_units(self) -> None:
        self.assertEqual(human_bytes(0), "0B")
        self.assertEqual(human_bytes(1536), "1.50K")
        self.assertEqual(human_rate(1024), "1.00K/s")
        self.assertEqual(human_tokens(2_500_000), "2.50M")


if __name__ == "__main__":
    unittest.main()
