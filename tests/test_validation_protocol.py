from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aplan.validation_protocol import freeze_protocol, load_protocol, verify_protocol


class ValidationProtocolTests(unittest.TestCase):
    def test_project_protocol_is_frozen_and_verified(self) -> None:
        project = Path(__file__).resolve().parents[1]

        result = verify_protocol(project)

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["horizons"], [1, 5, 10, 20, 40, 60])
        self.assertEqual(result["baseline"], "纯量价横截面基线")
        self.assertTrue(result["research_only"])

    def test_changed_protocol_fails_lock_verification(self) -> None:
        source = Path(__file__).resolve().parents[1] / "config" / "validation_protocol.toml"
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = project / "config"
            config.mkdir()
            protocol = config / "validation_protocol.toml"
            protocol.write_bytes(source.read_bytes())
            freeze_protocol(project, change_reason="initial test freeze")
            protocol.write_text(
                protocol.read_text(encoding="utf-8").replace(
                    "minimum_oos_observations = 100",
                    "minimum_oos_observations = 99",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "锁未更新"):
                load_protocol(project)

            lock = json.loads(
                (config / "validation_protocol.lock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lock["change_reason"], "initial test freeze")


if __name__ == "__main__":
    unittest.main()
