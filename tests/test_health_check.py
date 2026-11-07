# SPDX-License-Identifier: GPL-3.0-or-later
import ast
import datetime
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from health_check import app  # noqa: E402


class RunHelperTests(unittest.TestCase):
    def test_missing_command_and_timeout_are_reported(self):
        with patch.object(app.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(app.run(["missing-tool"]), (127, ""))
        with patch.object(app.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(["slow-tool"], 1)):
            self.assertEqual(app.run(["slow-tool"], timeout=1), (124, ""))


class CheckTests(unittest.TestCase):
    def test_extensions_missing_tool_and_empty_list(self):
        with patch.object(app.shutil, "which", return_value=None):
            self.assertEqual(app.check_extensions()["level"], "info")
        with patch.object(app.shutil, "which", return_value="/usr/bin/gnome-extensions"), \
                patch.object(app, "run", return_value=(0, "")):
            result = app.check_extensions()
        self.assertEqual(result["level"], "ok")
        self.assertEqual(result["fix"], [])

    def test_extensions_validate_id_and_offer_targeted_and_emergency_fixes(self):
        with patch.object(app.shutil, "which", return_value="/usr/bin/gnome-extensions"), \
                patch.object(app, "run", side_effect=[
                    (0, "broken@sample.test;touch\nbroken@sample.test"),
                    (0, "State: ERROR"),
                ]) as mocked_run:
            result = app.check_extensions()
        self.assertEqual(len(result["fix"]), 2)
        self.assertEqual(result["fix"][0]["steps"],
                         [["gnome-extensions", "disable", "broken@sample.test"]])
        self.assertEqual(result["fix"][1]["steps"][0][-1], "true")
        self.assertEqual(mocked_run.call_count, 2)
        self.assertNotIn("touch", str(result["fix"]))

    def test_extensions_do_not_call_unknown_status_healthy(self):
        with patch.object(app.shutil, "which", return_value="/usr/bin/gnome-extensions"), \
                patch.object(app, "run", side_effect=[(0, "sample@sample.test"),
                                                       (0, "No state listed")]):
            result = app.check_extensions()
        self.assertEqual(result["level"], "info")

    def test_crashes_missing_tool_and_empty_output(self):
        with patch.object(app.shutil, "which", return_value=None):
            missing = app.check_crashes()
        self.assertEqual(missing["fix"], [])
        self.assertEqual(missing["level"], "info")
        with patch.object(app.shutil, "which", return_value="/usr/bin/coredumpctl"), \
                patch.object(app, "run", return_value=(0, "")):
            self.assertEqual(app.check_crashes()["level"], "ok")

    def test_hardware_empty_output(self):
        with patch.object(app, "run", return_value=(0, "")):
            result = app.check_hardware()
        self.assertEqual(result["level"], "ok")
        self.assertEqual(result["fix"], [])

    def test_updates_empty_output_missing_tool_and_pending_updates(self):
        with patch.object(app, "run", return_value=(0, "")):
            self.assertEqual(app.check_updates()["level"], "ok")
        with patch.object(app, "run", return_value=(127, "")):
            self.assertEqual(app.check_updates()["level"], "info")
        with patch.object(app, "run",
                          return_value=(0, "sample/pkg 1.1 amd64 [upgradable from: 1.0]")):
            result = app.check_updates()
        self.assertEqual(result["fix"], [])

    def test_packages_empty_output_and_broken_packages(self):
        with patch.object(app, "run", return_value=(0, "")):
            self.assertEqual(app.check_packages()["level"], "ok")
        with patch.object(app, "run", return_value=(0, "package needs configuration")):
            result = app.check_packages()
        self.assertEqual(len(result["fix"][0]["steps"]), 1)
        self.assertTrue(result["fix"][0]["needs_root"])

    def test_disk_space_warning_has_safe_package_cleanup(self):
        usage = SimpleNamespace(total=100_000_000_000, used=90_000_000_000)
        with patch.object(app.shutil, "disk_usage", return_value=usage):
            result = app.check_disk()
        self.assertEqual(result["level"], "bad")
        commands = result["fix"][0]["steps"]
        self.assertEqual(commands[0][-1], "autoremove")
        self.assertEqual(commands[1][-1], "clean")
        self.assertIn("personal files", result["help"])

    def test_memory_valid_and_missing_fields(self):
        contents = "MemTotal: 1000 kB\nMemAvailable: 200 kB\n"
        with patch("builtins.open", mock_open(read_data=contents)):
            self.assertEqual(app.check_memory()["level"], "ok")
        with patch("builtins.open", mock_open(read_data="MemTotal: 1000 kB\n")):
            self.assertEqual(app.check_memory()["level"], "info")

    def test_services_validate_restart_name(self):
        with patch.object(app, "run", side_effect=[
                (0, "bad.service loaded failed failed a service"),
                (0, ""),
        ]):
            valid = app.check_services()
        self.assertEqual(valid["fix"][0]["steps"], [["systemctl", "restart", "bad.service"]])
        self.assertTrue(valid["fix"][0]["needs_root"])
        with patch.object(app, "run", side_effect=[
                (0, "bad;touch.service loaded failed failed a service"),
                (0, ""),
        ]):
            unsafe = app.check_services()
        self.assertEqual(unsafe["fix"], [])

    def test_services_report_when_one_scope_cannot_be_checked(self):
        with patch.object(app, "run", side_effect=[(127, ""), (0, "")]):
            self.assertEqual(app.check_services()["level"], "info")

    def test_log_hides_known_messages_and_counts_them(self):
        logs = "\n".join([
            "bluetoothd[1]: sap-server failed",
            "bluetoothd[1]: sap_server_register failed",
            "pam: gkr-pam: unable to locate daemon control file",
            "gdm: GDM_IS_REMOTE_DISPLAY assertion failed",
            "systemd: Failed to start gnome-keyring secrets",
            r"systemd: Failed to start app-gnome\x2dkeyring\x2dssh.scope",
            "systemd: Failed to start app-spice-vdagent.scope",
            "kernel: SGX disabled or unsupported by BIOS",
            "kernel: nvme0n1 AER corrected error",
            "kernel: wireless-device0 AER corrected error",
            "systemd: unusual problem",
        ])
        with patch.object(app, "run", return_value=(0, logs)):
            result = app.check_log_errors()
        self.assertIn("One unusual error", result["summary"])
        self.assertIn("10 known harmless", result["summary"])
        self.assertEqual(result["detail"], "systemd: unusual problem")

    def test_log_keeps_repeated_aer_messages_visible(self):
        logs = "kernel: nvme0n1 AER corrected error\nkernel: nvme0n1 AER corrected error"
        with patch.object(app, "run", return_value=(0, logs)):
            result = app.check_log_errors()
        self.assertIn("2 unusual error", result["summary"])
        self.assertEqual(result["detail"].count("AER"), 2)

    def test_hardware_check_groups_aer_messages_by_log_device_name(self):
        logs = "\n".join([
            "kernel: nvme0 0000:01:00.0: AER corrected error",
            "kernel: nvme0 0000:01:00.0: AER corrected error",
            "kernel: wifi0 0000:02:00.0: AER corrected error",
        ])
        with patch.object(app, "run", return_value=(0, logs)):
            result = app.check_hardware()
        self.assertEqual(result["level"], "info")
        self.assertIn("2 devices", result["summary"])
        self.assertIn("0000:01:00.0: 2", result["detail"])
        self.assertIn("0000:02:00.0: 1", result["detail"])

    def test_hardware_check_is_ok_without_aer_messages_or_wifi_device(self):
        with patch.object(app, "run", return_value=(0, "kernel: boot completed")):
            result = app.check_hardware()
        self.assertEqual(result["level"], "ok")
        self.assertEqual(result["fix"], [])

    def test_internal_drive_detection_accepts_sata_without_nvme(self):
        output = '{"blockdevices": [{"name": "sda", "type": "disk", "tran": "sata", "rm": false}]}'
        self.assertEqual(app.internal_drives(output), ["sda"])
        self.assertIsNone(app.internal_drives("not valid JSON"))
        unsafe_names = '{"blockdevices": [' \
            '{"name": "../sda", "type": "disk", "rm": false},' \
            '{"name": "-help", "type": "disk", "rm": false}]}'
        self.assertEqual(app.internal_drives(unsafe_names), [])
        with patch.object(app.shutil, "which", return_value="/usr/sbin/smartctl"), \
                patch.object(app, "run", side_effect=[
                    (0, output),
                    (0, "SMART overall-health self-assessment test result: PASSED"),
                ]) as mocked_run:
            result = app.check_drive()
        self.assertEqual(result["level"], "ok")
        self.assertEqual(mocked_run.call_args_list[1].args[0][-1], "/dev/sda")

    def test_drive_check_checks_all_detected_internal_drives(self):
        listing = '{"blockdevices": [' \
            '{"name": "sda", "type": "disk", "tran": "sata", "rm": false},' \
            '{"name": "nvme0n1", "type": "disk", "tran": "nvme", "rm": false},' \
            '{"name": "sdb", "type": "disk", "tran": "usb", "rm": false}]}'
        smart_report = "SMART overall-health self-assessment test result: PASSED"
        with patch.object(app.shutil, "which", return_value="/usr/sbin/smartctl"), \
                patch.object(app, "run", side_effect=[
                    (0, listing), (0, smart_report), (0, smart_report),
                ]) as mocked_run:
            result = app.check_drive()
        self.assertEqual(result["level"], "ok")
        self.assertIn("All 2 internal drives", result["summary"])
        self.assertEqual(mocked_run.call_args_list[1].args[0][-1], "/dev/sda")
        self.assertEqual(mocked_run.call_args_list[2].args[0][-1], "/dev/nvme0n1")

    def test_drive_check_reports_smart_unsupported_and_no_drive_sensibly(self):
        with patch.object(app.shutil, "which", return_value=None):
            missing = app.check_drive()
        self.assertEqual(missing["id"], "drive")
        self.assertEqual(missing["fix"], [])
        with patch.object(app.shutil, "which", return_value="/usr/sbin/smartctl"), \
                patch.object(app, "run", return_value=(0, '{"blockdevices": []}')):
            empty = app.check_drive()
        self.assertEqual(empty["level"], "info")
        self.assertIn("No internal drives", empty["summary"])
        listing = '{"blockdevices": [{"name": "sda", "type": "disk", "tran": "sata", "rm": false}]}'
        with patch.object(app.shutil, "which", return_value="/usr/sbin/smartctl"), \
                patch.object(app, "run", side_effect=[
                    (0, listing), (2, "Device does not support SMART"),
                ]):
            unsupported = app.check_drive()
        self.assertEqual(unsupported["level"], "info")
        self.assertIn("does not support SMART", unsupported["detail"])

    def test_dynamic_command_names_are_validated_and_shell_is_never_enabled(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "run" and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "subprocess":
                self.assertFalse(any(
                    keyword.arg == "shell" and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords))
        with patch.object(app.shutil, "which", return_value="/usr/bin/gnome-extensions"), \
                patch.object(app, "run", side_effect=[
                    (0, "safe@sample.test;touch\nsafe@sample.test"),
                    (0, "State: ERROR"),
                ]):
            extension_fix = app.check_extensions()["fix"][0]["steps"][0][-1]
        self.assertRegex(extension_fix, app.EXTENSION_ID_RE)
        with patch.object(app, "run", side_effect=[
                (0, "safe.service loaded failed failed a service"), (0, ""),
        ]):
            service_fix = app.check_services()["fix"][0]["steps"][0][-1]
        self.assertRegex(service_fix, app.SERVICE_NAME_RE)
        self.assertRegex("sda", app.BLOCK_DEVICE_RE)

    def test_application_source_does_not_import_network_clients(self):
        forbidden = {"urllib", "requests", "socket", "http"}
        for path in PROJECT_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = {alias.name.split(".", 1)[0] for alias in node.names}
                    self.assertTrue(forbidden.isdisjoint(imported), path.name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".", 1)[0]
                    self.assertNotIn(root, forbidden, path.name)


class FixTests(unittest.TestCase):
    def test_root_apt_fix_uses_pkexec_and_noninteractive_argv(self):
        action = app.fix("Clean cached packages.",
                         [app.apt_step("clean")], needs_root=True)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(app.subprocess, "run", return_value=completed) as mocked_run:
            success, _message, _output = app.run_fix(action)
        self.assertTrue(success)
        argv = mocked_run.call_args.args[0]
        self.assertEqual(argv[:4], ["pkexec", "env", "DEBIAN_FRONTEND=noninteractive",
                                    "apt-get"])
        self.assertIs(mocked_run.call_args.kwargs["shell"], False)

    def test_cancelled_password_and_locked_package_tool_get_friendly_messages(self):
        action = app.fix("Restart task.", [["systemctl", "restart", "demo.service"]],
                         needs_root=True)
        cancelled = subprocess.CompletedProcess([], 126, "", "")
        with patch.object(app.subprocess, "run", return_value=cancelled):
            success, message, _output = app.run_fix(action)
        self.assertFalse(success)
        self.assertIn("cancelled", message)

        locked = subprocess.CompletedProcess(
            [], 100, "", "Could not get lock /var/lib/dpkg/lock-frontend")
        with patch.object(app.subprocess, "run", return_value=locked):
            success, message, _output = app.run_fix(action)
        self.assertFalse(success)
        self.assertIn("updating your computer", message)

    def test_multi_step_repairs_continue_after_a_failed_first_step(self):
        action = app.fix("Repair packages.",
                         [["dpkg", "--configure", "-a"],
                          app.apt_step("-f", "install")],
                         needs_root=True)
        first = subprocess.CompletedProcess([], 1, "", "Some packages need attention.")
        second = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(app.subprocess, "run", side_effect=[first, second]) as mocked_run:
            success, _message, output = app.run_fix(action)
        self.assertFalse(success)
        self.assertIn("Some packages need attention.", output)
        self.assertEqual(mocked_run.call_count, 2)


class SetupTests(unittest.TestCase):
    def test_install_lists_exact_missing_package_command(self):
        output = StringIO()
        with patch.object(app, "package_installed", side_effect=[True, False, True]), \
                redirect_stdout(output):
            status = app.install()
        self.assertEqual(status, 1)
        self.assertIn("sudo apt install gir1.2-gtk-4.0", output.getvalue())

    def test_missing_gtk_libraries_print_a_friendly_install_message(self):
        error = StringIO()
        with patch.dict(sys.modules, {"gi": None}), redirect_stderr(error):
            status = app.run_gui()
        self.assertEqual(status, 1)
        self.assertIn("sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1",
                      error.getvalue())

    def test_install_writes_daily_menu_and_sign_in_entries_and_uninstall_removes_files(self):
        with tempfile.TemporaryDirectory(prefix="Desktop Checkup ") as folder:
            data_dir = os.path.join(folder, "data")
            state_file = os.path.join(data_dir, "state.json")
            app_entry = os.path.join(
                folder, "applications", "io.github.dariocostanzo.DesktopCheckup.desktop")
            auto_entry = os.path.join(
                folder, "autostart", "desktop-checkup-reminder.desktop")
            with patch.object(app, "DATA_DIR", data_dir), \
                    patch.object(app, "STATE_FILE", state_file), \
                    patch.object(app, "desktop_paths", return_value=(app_entry, auto_entry)), \
                    patch.object(app, "package_installed", return_value=True), \
                    redirect_stdout(StringIO()):
                self.assertEqual(app.install(), 0)
                with open(app_entry) as entry:
                    menu_text = entry.read()
                with open(auto_entry) as entry:
                    auto_text = entry.read()
                self.assertIn('Exec="', menu_text)
                self.assertIn("--daily", auto_text)
                self.assertIn(os.path.realpath(app.__file__), menu_text)
                self.assertFalse(os.path.exists(
                    os.path.join(data_dir, "health_check.py")))
                app.uninstall()
            self.assertFalse(os.path.exists(app_entry))
            self.assertFalse(os.path.exists(auto_entry))
            self.assertFalse(os.path.exists(data_dir))

    def test_daily_check_notifies_only_for_actionable_problems(self):
        warning = app.result("upd", "Updates", "warn", "Updates are ready.")
        with patch.object(app, "run_all", return_value=[warning]), \
                patch.object(app, "save_state") as save_state, \
                patch.object(app, "notify", return_value=(0, "")) as notify:
            self.assertEqual(app.daily(), 0)
        save_state.assert_called_once_with([warning])
        notify.assert_called_once()

        with patch.object(app, "run_all", return_value=[
                app.result("drive", "Drive health", "info", "Not checked.")]), \
                patch.object(app, "save_state"), \
                patch.object(app, "notify") as notify:
            self.assertEqual(app.daily(), 0)
        notify.assert_not_called()

    def test_monthly_reminder_observes_saved_check_date(self):
        now = datetime.datetime.now()
        with patch.object(app, "load_state",
                          return_value={"time": now.isoformat()}), \
                patch.object(app, "notify") as notify:
            app.remind()
        notify.assert_not_called()

        old = (now - datetime.timedelta(days=app.REMIND_DAYS + 1)).isoformat()
        with patch.object(app, "load_state", return_value={"time": old}), \
                patch.object(app, "notify", return_value=(0, "")) as notify:
            app.remind()
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
