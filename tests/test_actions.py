"""Validation of the actions allowlist file (Goal: refuse to start on any error)."""

import copy
import json
import os
import tempfile
import unittest

from tests import fakes  # noqa: F401  (installs the real-device open guard)
from tests.fakes import TEST_ACTIONS_DATA

from flipper_subghz_api.__main__ import main
from flipper_subghz_api.actions import (
    MAX_ACTIONS, ActionsError, Allowlist, load_actions, parse_actions,
)
from flipper_subghz_api.config import ConfigError, load_config


def entry(name="Gate", path=None, protocol="RAW", repeat=1, **extra):
    e = {"name": name, "path": path or f"/ext/subghz/{name}.sub", "protocol": protocol,
         "repeat": repeat}
    e.update(extra)
    return e


def doc(*entries):
    return {"actions": list(entries)}


class ParseActionsTests(unittest.TestCase):
    def assert_rejected(self, data, fragment=None):
        with self.assertRaises(ActionsError) as cm:
            parse_actions(data)
        if fragment:
            self.assertIn(fragment, str(cm.exception))
        return cm.exception

    def test_valid_document(self):
        al = parse_actions(copy.deepcopy(TEST_ACTIONS_DATA))
        self.assertIsInstance(al, Allowlist)
        self.assertEqual(al.names, ("Gate", "Garage_door", "Light_on", "Light_off"))
        self.assertEqual(al.specs["Light_on"].repeat, 10)
        self.assertEqual(al.by_path["/ext/subghz/Gate.sub"].protocol, "RAW")

    def test_allowlist_is_detached_from_input(self):
        data = copy.deepcopy(TEST_ACTIONS_DATA)
        al = parse_actions(data)
        data["actions"][0]["path"] = "/ext/subghz/Other.sub"
        data["actions"].append(entry("Late"))
        self.assertEqual(al.specs["Gate"].path, "/ext/subghz/Gate.sub")
        self.assertNotIn("Late", al.specs)

    def test_name_rules(self):
        for bad in ("", "a" * 33, "Gate-1", "Gate 1", "Gate/..", "Gät", "Gate.sub",
                    "Gate\n", 7, None, ["Gate"]):
            with self.subTest(name=bad):
                self.assert_rejected(doc(entry(name=bad, path="/ext/subghz/X.sub")), "name")
        parse_actions(doc(entry(name="a" * 32, path="/ext/subghz/X.sub")))  # bound ok

    def test_path_rules(self):
        cases = {
            "outside /ext/subghz": "/ext/infrared/Gate.sub",
            "other root": "/int/subghz/Gate.sub",
            "relative": "ext/subghz/Gate.sub",
            "dotdot": "/ext/subghz/../Gate.sub",
            "dotdot name": "/ext/subghz/..sub",
            "nested dotdot": "/ext/subghz/x/../Gate.sub",
            "subdirectory": "/ext/subghz/remotes/Gate.sub",
            "space": "/ext/subghz/My Gate.sub",
            "double quote": '/ext/subghz/"Gate".sub',
            "single quote": "/ext/subghz/Gate's.sub",
            "trailing space": "/ext/subghz/Gate.sub ",
            "newline injection": "/ext/subghz/Gate.sub\nstorage remove /ext",
            "cr injection": "/ext/subghz/Gate.sub\r",
            "args injection": "/ext/subghz/Gate.sub 50 0",
            "wrong suffix": "/ext/subghz/Gate.txt",
            "no stem": "/ext/subghz/.sub",
            "uppercase suffix": "/ext/subghz/Gate.SUB",
            "unicode": "/ext/subghz/Gäte.sub",
            "not a string": 42,
        }
        for label, bad in cases.items():
            with self.subTest(label):
                self.assert_rejected(doc(entry(path=bad)), "path")

    def test_protocol_rules(self):
        for bad in ("", "a" * 33, "RAW 1", "Prince-ton", "RAW\r", None, 1):
            with self.subTest(protocol=bad):
                self.assert_rejected(doc(entry(protocol=bad)), "protocol")
        parse_actions(doc(entry(protocol="Came_Atomo")))  # arbitrary safe names allowed

    def test_repeat_bounds(self):
        for ok in (1, 50):
            with self.subTest(ok=ok):
                self.assertEqual(parse_actions(doc(entry(repeat=ok))).specs["Gate"].repeat, ok)
        for bad in (0, -1, 51, 1000, 1.0, "1", True, None):
            with self.subTest(repeat=bad):
                self.assert_rejected(doc(entry(repeat=bad)), "repeat")

    def test_duplicate_names_rejected(self):
        self.assert_rejected(doc(entry("Gate", path="/ext/subghz/A.sub"),
                                 entry("Gate", path="/ext/subghz/B.sub")), "duplicate action")
        self.assert_rejected(doc(entry("Gate", path="/ext/subghz/A.sub"),
                                 entry("gate", path="/ext/subghz/B.sub")), "duplicate action")

    def test_duplicate_paths_rejected(self):
        self.assert_rejected(doc(entry("A", path="/ext/subghz/Gate.sub"),
                                 entry("B", path="/ext/subghz/Gate.sub")), "duplicate path")
        self.assert_rejected(doc(entry("A", path="/ext/subghz/Gate.sub"),
                                 entry("B", path="/ext/subghz/gate.sub")), "duplicate path")

    def test_entry_count_bounds(self):
        self.assert_rejected(doc(), "between 1 and")
        many = [entry(f"A{i}") for i in range(MAX_ACTIONS)]
        self.assertEqual(len(parse_actions(doc(*many))), MAX_ACTIONS)
        self.assert_rejected(doc(*many, entry("Overflow")), "between 1 and")

    def test_structure_rules(self):
        cases = {
            "not an object": [entry()],
            "missing actions": {},
            "extra top-level key": {"actions": [entry()], "comment": "x"},
            "actions not a list": {"actions": entry()},
            "entry not an object": {"actions": ["Gate"]},
            "missing key": {"actions": [{"name": "Gate", "path": "/ext/subghz/Gate.sub",
                                         "protocol": "RAW"}]},
            "unknown key": {"actions": [entry(device=1)]},
            "typo key": {"actions": [{"name": "Gate", "path": "/ext/subghz/Gate.sub",
                                      "protocol": "RAW", "repeats": 1}]},
        }
        for label, data in cases.items():
            with self.subTest(label):
                self.assert_rejected(data)

    def test_actions_error_is_a_config_error(self):
        self.assertTrue(issubclass(ActionsError, ConfigError))


class LoadActionsFileTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.d.cleanup()

    def write(self, name, text, mode=0o644):
        p = os.path.join(self.d.name, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(p, mode)
        return p

    def test_valid_json_file(self):
        p = self.write("actions.json", json.dumps(TEST_ACTIONS_DATA))
        self.assertEqual(len(load_actions(p)), 4)

    def test_valid_toml_file(self):
        p = self.write("actions.toml", (
            '[[actions]]\nname = "Gate"\npath = "/ext/subghz/Gate.sub"\n'
            'protocol = "RAW"\nrepeat = 1\n\n'
            '[[actions]]\nname = "Light_on"\npath = "/ext/subghz/Light_on.sub"\n'
            'protocol = "Princeton"\nrepeat = 10\n'))
        al = load_actions(p)
        self.assertEqual(al.names, ("Gate", "Light_on"))
        self.assertEqual(al.specs["Light_on"].repeat, 10)

    def test_shipped_example_file_is_valid(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "actions.example.json"), encoding="utf-8") as fh:
            p = self.write("example.json", fh.read())  # checkout mode depends on umask
        al = load_actions(p)
        self.assertEqual(al.names, ("Gate", "Garage_door", "Light_on", "Light_off",
                                    "Socket_1"))

    def test_file_level_refusals(self):
        cases = {
            "missing": os.path.join(self.d.name, "nope.json"),
            "directory": self.d.name,
            "group writable": self.write("gw.json", json.dumps(TEST_ACTIONS_DATA), 0o664),
            "world writable": self.write("ww.json", json.dumps(TEST_ACTIONS_DATA), 0o646),
            "bad json": self.write("bad.json", '{"actions": ['),
            "bad toml": self.write("bad.toml", "[[actions]\n"),
            "duplicate json key": self.write("dup.json", (
                '{"actions": [{"name": "Gate", "path": "/ext/subghz/Gate.sub", '
                '"path": "/ext/subghz/../x.sub", "protocol": "RAW", "repeat": 1}]}')),
            "too large": self.write("big.json", " " * (64 * 1024 + 1)),
            "invalid content": self.write("inv.json", json.dumps(doc(entry(repeat=0)))),
        }
        for label, p in cases.items():
            with self.subTest(label), self.assertRaises(ActionsError):
                load_actions(p)

    def test_main_refuses_to_start_on_invalid_actions(self):
        env = self.write("config.env", "FLIPPER_API_TOKEN=" + "t" * 40
                         + "\nFLIPPER_API_BIND=127.0.0.1\n", mode=0o600)
        bad = self.write("bad.json", json.dumps(doc(entry(path="/ext/subghz/../Gate.sub"))))
        good = self.write("good.json", json.dumps(TEST_ACTIONS_DATA))
        self.assertEqual(main(["--env-file", env, "--actions-file", bad, "--check-config"]), 2)
        self.assertEqual(main(["--env-file", env, "--actions-file", bad]), 2)  # not served
        self.assertEqual(main(["--env-file", env, "--actions-file", good,
                               "--check-config"]), 0)

    def test_actions_file_precedence(self):
        env = self.write("config.env", "FLIPPER_API_TOKEN=" + "t" * 40
                         + "\nFLIPPER_API_BIND=127.0.0.1\nFLIPPER_ACTIONS_FILE=/from/envfile\n",
                         mode=0o600)
        self.assertEqual(load_config(env, environ={}).actions_file, "/from/envfile")
        self.assertEqual(load_config(env, environ={"FLIPPER_ACTIONS_FILE": "/from/env"})
                         .actions_file, "/from/env")
        self.assertEqual(load_config(env, actions_file="/from/flag",
                                     environ={"FLIPPER_ACTIONS_FILE": "/from/env"})
                         .actions_file, "/from/flag")


if __name__ == "__main__":
    unittest.main()
