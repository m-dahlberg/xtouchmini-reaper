import tempfile
import unittest
from pathlib import Path

from xtouchmini.config import Config, load, validate
from xtouchmini.protocol import Encoding


def write(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "config.toml"
    p.write_text(text)
    return p


class TestLoad(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        cfg = load(Path("/nonexistent/config.toml"))
        self.assertEqual(cfg.osc.send_port, 8010)
        self.assertEqual(cfg.encoding, Encoding.RELATIVE_1)

    def test_ports_do_not_collide_with_shuttlexpress(self):
        # Port 8000 and csurf_0 belong to the ShuttleXpress bridge.
        cfg = Config()
        self.assertNotEqual(cfg.osc.send_port, 8000)
        self.assertNotEqual(cfg.osc.listen_port, 8000)

    def test_partial_override(self):
        cfg = load(write('[osc]\nsend_port = 9999\n'))
        self.assertEqual(cfg.osc.send_port, 9999)
        self.assertEqual(cfg.osc.host, "127.0.0.1")

    def test_unknown_key_is_rejected(self):
        # A typo that silently did nothing would be worse than an error.
        with self.assertRaises(ValueError) as cm:
            load(write('[osc]\nsend_prot = 9999\n'))
        self.assertIn("send_prot", str(cm.exception))

    def test_unknown_section_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            load(write('[oscilloscope]\nx = 1\n'))
        self.assertIn("oscilloscope", str(cm.exception))

    def test_malformed_toml_names_the_file(self):
        path = write('[osc\n')
        with self.assertRaises(ValueError) as cm:
            load(path)
        self.assertIn(str(path), str(cm.exception))

    def test_all_encodings_load(self):
        for enc in Encoding:
            cfg = load(write(f'[device]\nencoding = "{enc.value}"\n'))
            self.assertEqual(cfg.encoding, enc)


class TestValidate(unittest.TestCase):
    def test_bad_encoding_lists_the_valid_ones(self):
        with self.assertRaises(ValueError) as cm:
            load(write('[device]\nencoding = "relative9"\n'))
        self.assertIn("relative1", str(cm.exception))

    def test_identical_ports_are_rejected(self):
        # They would form a loop: our own writes would return as feedback.
        with self.assertRaises(ValueError) as cm:
            load(write('[osc]\nsend_port = 9010\nlisten_port = 9010\n'))
        self.assertIn("differ", str(cm.exception))

    def test_port_range(self):
        for bad in ("0", "70000"):
            with self.assertRaises(ValueError):
                load(write(f'[osc]\nsend_port = {bad}\n'))

    def test_fine_step_must_be_finer(self):
        with self.assertRaises(ValueError) as cm:
            load(write('[feel]\ncoarse_step = 0.001\nfine_step = 0.5\n'))
        self.assertIn("fine_step", str(cm.exception))

    def test_non_positive_steps_are_rejected(self):
        with self.assertRaises(ValueError):
            load(write('[feel]\ncoarse_step = 0.0\n'))

    def test_pickup_epsilon_range(self):
        with self.assertRaises(ValueError):
            load(write('[feel]\npickup_epsilon = 1.5\n'))

    def test_defaults_are_valid(self):
        validate(Config())


if __name__ == "__main__":
    unittest.main()
