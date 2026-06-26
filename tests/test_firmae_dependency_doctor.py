from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.firmae.scripts import verify_dependencies


class FirmAEDependencyDoctorTests(unittest.TestCase):
    def test_missing_command_is_reported(self) -> None:
        with patch.object(
            verify_dependencies,
            "REQUIRED_COMMANDS",
            ("present-tool", "missing-tool"),
        ), patch.object(
            verify_dependencies,
            "REQUIRED_FILES",
            (),
        ), patch.object(
            verify_dependencies,
            "REQUIRED_IMPORTS",
            (),
        ), patch(
            "adapters.firmae.scripts.verify_dependencies.shutil.which",
            side_effect=lambda command: (
                "/usr/bin/present-tool"
                if command == "present-tool"
                else None
            ),
        ):
            report = verify_dependencies.inspect_dependencies()

        self.assertEqual(report["status"], "fail")
        self.assertEqual(
            report["missing_commands"],
            ["missing-tool"],
        )

    def test_available_dependencies_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            required_file = Path(temporary) / "required"
            required_file.write_text(
                "present\n",
                encoding="utf-8",
            )

            with patch.object(
                verify_dependencies,
                "REQUIRED_COMMANDS",
                ("present-tool",),
            ), patch.object(
                verify_dependencies,
                "REQUIRED_FILES",
                (str(required_file),),
            ), patch.object(
                verify_dependencies,
                "REQUIRED_IMPORTS",
                ("json",),
            ), patch(
                "adapters.firmae.scripts.verify_dependencies.shutil.which",
                return_value="/usr/bin/present-tool",
            ):
                report = verify_dependencies.inspect_dependencies()

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["missing_commands"], [])
        self.assertEqual(report["missing_files"], [])
        self.assertEqual(report["missing_python_imports"], [])


if __name__ == "__main__":
    unittest.main()
