"""Offline dashboard integration without a browser or model server."""
import unittest
from pathlib import Path
from streamlit.testing.v1 import AppTest


class DashboardTests(unittest.TestCase):
    def test_render_tick_and_reset(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py"), default_timeout=45).run()
        self.assertEqual(len(app.exception),0)
        next(b for b in app.sidebar.button if b.label == "Advance tick and process incidents").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(app.session_state["sim"].state["tick"],1)
        self.assertTrue(app.session_state["sim"].state["message_board"])
        self.assertEqual(len(app.dataframe),2)
        next(b for b in app.sidebar.button if b.label == "Reset Seed 42 scenario").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(app.session_state["sim"].state["tick"],0)
