from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aplan.audit import verify_audit_chain, write_chained_audit


class AuditTests(unittest.TestCase):
    def test_chain_passes_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            first = write_chained_audit(project, "20260701", {"status": "passed"})
            write_chained_audit(project, "20260702", {"status": "passed"})
            self.assertTrue(verify_audit_chain(project)["passed"])
            first.write_text(json.dumps({"status": "changed"}), encoding="utf-8")
            result = verify_audit_chain(project)
            self.assertFalse(result["passed"])
            self.assertTrue(any("哈希不匹配" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()

