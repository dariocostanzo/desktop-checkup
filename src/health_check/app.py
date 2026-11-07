# SPDX-License-Identifier: GPL-3.0-or-later
"""A local desktop health check for supported Ubuntu and Zorin systems."""
import datetime
import gettext
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading

_ = gettext.translation("desktop-checkup", fallback=True).gettext

APP_ID = "io.github.dariocostanzo.DesktopCheckup"
DATA_DIR = os.path.expanduser("~/.local/share/desktop-checkup")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
REMIND_DAYS = 30
ENV = dict(os.environ, LC_ALL="C")
ORDER = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
GUI_PACKAGES = ("python3-gi", "gir1.2-gtk-4.0", "gir1.2-adw-1")
EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+$")
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
BLOCK_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
AER_RE = re.compile(r"\bAER\b", re.IGNORECASE)
DEVICE_TOKEN_RE = re.compile(
    r"(?<![\w.-])(?:[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]"
    r"|[A-Za-z][A-Za-z0-9_.-]*)(?=[:\s]|$)")


# ---------------------------------------------------------------- helpers

def run(cmd, timeout=60):
    """Run a command and return (exit code, stdout). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=ENV, shell=False, check=False)
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""


def fix(description, steps, needs_root=False, label="Fix"):
    return {"description": _(description), "steps": steps,
            "needs_root": needs_root, "label": _(label)}


def result(id_, title, level, summary, help_="", fix=None, detail=""):
    return {"id": id_, "title": _(title), "level": level, "summary": _(summary),
            "help": _(help_), "fix": fix or [], "detail": _(detail)}


def gb(n):
    return f"{n / 1e9:.1f} GB"


def apt_step(*args):
    return ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-y",
            "-o", "DPkg::Lock::Timeout=0", *args]


def aer_device(line):
    match = AER_RE.search(line)
    if not match:
        return None
    devices = DEVICE_TOKEN_RE.findall(line[:match.start()])
    if not devices or devices[-1].lower() in {"kernel", "kern"}:
        return "unknown device"
    return devices[-1]


def internal_drives(lsblk_output):
    try:
        data = json.loads(lsblk_output)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("blockdevices"), list):
        return None
    names = []
    for device in data["blockdevices"]:
        if not isinstance(device, dict):
            continue
        name = device.get("name")
        transport = (device.get("tran") or "").lower()
        removable = device.get("rm") in (True, 1, "1")
        if (device.get("type") == "disk" and not removable
                and transport != "usb" and isinstance(name, str)
                and BLOCK_DEVICE_RE.fullmatch(name)):
            names.append(name)
    return names


# ----------------------------------------------------------------- checks

def check_extensions():
    title = "Desktop add-ons"
    if not shutil.which("gnome-extensions"):
        return result("ext", title, "info", "Could not check your desktop add-ons.")
    rc, out = run(["gnome-extensions", "list", "--user"])
    if rc != 0:
        return result("ext", title, "info", "Could not check your desktop add-ons.",
                      detail=out.strip()[:1500])
    names = out.split()
    if not names:
        return result("ext", title, "ok", "You have no extra desktop add-ons.")
    valid_names = [name for name in names if EXTENSION_ID_RE.fullmatch(name)]
    if not valid_names:
        return result("ext", title, "info",
                      "Could not safely check the listed desktop add-ons.")
    lines, bad, warn, unknown = [], [], [], []
    for n in valid_names:
        info_rc, info = run(["gnome-extensions", "info", n])
        if info_rc != 0:
            lines.append(f"{n}: Could not read its status\n{info.strip()[:500]}")
            unknown.append(n)
            continue
        m = re.search(r"State:\s*(.+)", info)
        state = m.group(1).strip() if m else "UNKNOWN"
        lines.append(f"{n}: {state}")
        if state.upper() == "ERROR":
            bad.append(n)
        elif state.upper() == "OUT OF DATE":
            warn.append(n)
        elif state.upper() == "UNKNOWN":
            unknown.append(n)
    detail = "\n".join(lines)
    if bad or warn:
        problem_names = bad + warn
        names_bad = ", ".join(problem_names)
        fixes = []
        if bad:
            safe_bad = [name for name in bad if EXTENSION_ID_RE.fullmatch(name)]
            if safe_bad:
                fixes.append(fix(
                    f"Switch off the add-on {safe_bad[0]}.",
                    [["gnome-extensions", "disable", safe_bad[0]]],
                    label="Switch off"))
            fixes.append(fix(
                "Switch off all the extra desktop add-ons. You can turn them on again later.",
                [["gsettings", "set", "org.gnome.shell",
                  "disable-user-extensions", "true"]],
                label="Switch off all"))
        return result(
            "ext", title, "bad" if bad else "warn",
            f"A problem was found with: {names_bad}",
            "A desktop add-on can make things look odd or stop working. "
            "Switching off a problem add-on may help. You can turn it on again later.",
            fixes, detail)
    if unknown:
        return result("ext", title, "info",
                      "Could not check the status of every desktop add-on.",
                      detail=detail)
    return result("ext", title, "ok",
                  f"All {len(valid_names)} desktop add-ons are working.",
                  detail=detail)


def check_crashes():
    title = "App crashes"
    if not shutil.which("coredumpctl"):
        return result(
            "crash", title, "info", "App crash recording is not set up.",
            "Adding this small system tool lets your computer keep a record "
            "when an app stops unexpectedly.")
    rc, out = run(["coredumpctl", "list", "--no-pager", "--no-legend",
                   "--since", "30 days ago"])
    text = out.strip()
    if rc != 0:
        return result("crash", title, "info", "Could not read the app crash report.",
                      detail=text[:1500])
    if not text or "No coredumps" in text:
        return result("crash", title, "ok",
                      "No apps have stopped unexpectedly in the last 30 days.")
    lines = text.splitlines()
    shell_crash = "gnome-shell" in text
    summary = ("One app stopped unexpectedly in the last 30 days"
               if len(lines) == 1
               else f"{len(lines)} apps stopped unexpectedly in the last 30 days")
    if shell_crash:
        summary += ", including the desktop."
    else:
        summary += "."
    return result(
        "crash", title, "bad" if shell_crash else "warn",
        summary,
        "An app has stopped unexpectedly. One crash can happen now and then. "
        "Repeated crashes of the same app, or of the desktop, may point to a "
        "problem with an add-on or update.",
        detail="\n".join(lines[-8:]))


def check_hardware():
    title = "Hardware messages"
    rc, out = run(["journalctl", "-k", "-b", "--no-pager", "-q"])
    if rc != 0 and not out:
        return result("hw", title, "info", "Could not read the system log.")
    counts = {}
    for line in out.splitlines():
        device = aer_device(line)
        if device:
            counts[device] = counts.get(device, 0) + 1
    total = sum(counts.values())
    detail = "\n".join(f"{device}: {count}" for device, count in sorted(counts.items()))
    if not total:
        return result("hw", title, "ok",
                      "No AER hardware messages were found in this session.")
    message_label = _("message") if total == 1 else _("messages")
    device_label = _("device") if len(counts) == 1 else _("devices")
    summary = _("{count} AER {message_label} from {device_count} "
                "{device_label} this session.").format(
                    count=total, message_label=message_label,
                    device_count=len(counts), device_label=device_label)
    why = ("AER messages describe errors reported by a hardware device. "
           "A few can be harmless, but repeated messages may mean a device "
           "or its connection needs attention.")
    return result("hw", title, "info" if total <= 50 else "warn",
                  summary, why, detail=detail)


def check_updates():
    title = "Updates"
    rc, out = run(["apt", "list", "--upgradable"])
    if rc == 127:
        return result("upd", title, "info", "Could not check for updates.",
                      detail="The update tool is not available.")
    if rc == 124:
        return result("upd", title, "info", "The update check took too long. Try again.")
    lines = [l for l in out.splitlines() if "upgradable" in l]
    n = len(lines)
    if rc != 0 and not lines:
        return result("upd", title, "info", "Could not check for updates.",
                      detail=out.strip()[:1500])
    if n == 0:
        return result("upd", title, "ok", "Your apps and system are up to date.",
                      detail="This uses the update list your computer last downloaded.")
    level = "warn" if n > 20 else "info"
    return result(
        "upd", title, level,
        f"{n} update{'s' if n != 1 else ''} ready to install.",
        "Updates fix problems and help keep your computer safe.",
        detail="\n".join(l.split("/")[0] for l in lines[:25]))


def check_packages():
    title = "Installed software"
    rc, out = run(["dpkg", "--audit"])
    if rc == 127:
        return result("pkg", title, "info", "Could not check your installed software.",
                      detail="The package check tool is not available.")
    if rc == 124:
        return result("pkg", title, "info", "The software check took too long. Try again.")
    if rc != 0 and not out.strip():
        return result("pkg", title, "info", "Could not check your installed software.")
    if not out.strip():
        return result("pkg", title, "ok", "No broken software packages.")
    return result(
        "pkg", title, "bad", "Some software was not installed properly.",
        "An unfinished installation can stop other updates. This repair "
        "finishes the setup and fixes missing package parts.",
        [fix("Finish and repair the interrupted software installation.",
             [["dpkg", "--configure", "-a"]], needs_root=True, label="Repair")],
        out.strip()[:1500])


def check_disk():
    title = "Storage space"
    try:
        u = shutil.disk_usage("/")
    except OSError as exc:
        return result("disk", title, "info", "Could not check storage space.",
                      detail=str(exc))
    pct = u.used / u.total * 100
    summary = f"{gb(u.used)} used of {gb(u.total)} ({pct:.0f}%)."
    if pct < 80:
        return result("disk", title, "ok", summary)
    help_ = ("A nearly full drive can slow things down. This clean-up removes "
             "unused app packages and their downloaded setup files. It does "
             "not remove your personal files.")
    return result("disk", title, "bad" if pct >= 90 else "warn", summary,
                  help_, [fix("Remove unused app packages and downloaded setup files.",
                              [apt_step("autoremove"), apt_step("clean")],
                              needs_root=True, label="Clean up")])


def check_memory():
    title = "Memory"
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError):
        return result("mem", title, "info", "Could not read memory use.")
    if not mem.get("MemTotal") or "MemAvailable" not in mem:
        return result("mem", title, "info", "Could not read memory use.")
    total, avail = mem["MemTotal"], mem["MemAvailable"]
    pct_free = avail / total * 100
    summary = f"{gb(avail)} available of {gb(total)}."
    if pct_free >= 10:
        return result("mem", title, "ok", summary)
    return result("mem", title, "bad" if pct_free < 5 else "warn", summary,
                  "Your computer is running low on memory, which can make it "
                  "slow. Close apps and browser tabs you are not using, or restart.")


def check_services():
    title = "Background tasks"
    failed = []
    checked = 0
    unavailable = 0
    for user in (False, True):
        cmd = ["systemctl"]
        if user:
            cmd.append("--user")
        cmd += ["--failed", "--no-legend", "--no-pager", "--plain"]
        rc, out = run(cmd)
        if rc != 0:
            unavailable += 1
            continue
        checked += 1
        for line in out.splitlines():
            fields = line.split()
            if fields:
                failed.append((fields[0], user))
    if not failed:
        if checked == 0 or unavailable:
            return result("svc", title, "info", "Could not check background tasks.")
        return result("svc", title, "ok", "All background tasks are running.")
    names = [name for name, _user in failed]
    count_text = (f"One background task did not start: {names[0]}"
                  if len(names) == 1
                  else f"{len(failed)} background tasks did not start: "
                       f"{', '.join(names[:4])}")
    fixes = []
    service_name, user_service = failed[0]
    if SERVICE_NAME_RE.fullmatch(service_name):
        argv = ["systemctl"]
        if user_service:
            argv.append("--user")
        argv += ["restart", service_name]
        fixes.append(fix(f"Restart the task {service_name}.", [argv],
                         needs_root=not user_service, label="Restart"))
    return result(
        "svc", title, "warn",
        count_text + (" and more." if len(failed) > 4 else "."),
        "Some background tasks are only needed for particular devices or "
        "apps. Restarting the first failed task may help.",
        fixes, "\n".join(names))


def check_log_errors():
    title = "Error messages in the system log"
    rc, out = run(["journalctl", "-b", "-p", "err", "--no-pager", "-q"])
    if rc == 127:
        return result("log", title, "info", "Could not read system messages.",
                      detail="The system log tool is not available.")
    if rc == 124:
        return result("log", title, "info", "The system message check took too long. Try again.")
    if rc != 0 and not out.strip():
        return result("log", title, "info", "Could not read system messages.")
    harmless = (
        re.compile(r"bluetoothd.*(?:sap-server|sap_server_register)", re.IGNORECASE),
        re.compile(r"gkr-pam.*unable to locate daemon control file", re.IGNORECASE),
        re.compile(r"GDM_IS_REMOTE_DISPLAY.*assert", re.IGNORECASE),
        re.compile(r"systemd.*(?:failed|failure).*start.*gnome[- ]keyring.*secrets",
                   re.IGNORECASE),
        re.compile(r"SGX disabled or unsupported by BIOS", re.IGNORECASE),
    )

    def is_harmless(line):
        normalized = line.lower().replace(r"\x2d", "-")
        known_scope = ("scope" in normalized and "app-" in normalized
                       and any(marker in normalized for marker in
                               ("gnome-keyring-ssh", "gnome-keyring-secrets",
                                "spice-vdagent")))
        return known_scope or any(pattern.search(line) for pattern in harmless)

    all_lines = out.splitlines()
    hidden_indices = {
        index for index, line in enumerate(all_lines)
        if is_harmless(line)
    }
    aer_groups = {}
    for index, line in enumerate(all_lines):
        device = aer_device(line)
        if device:
            aer_groups.setdefault(device, []).append(index)
    for indices in aer_groups.values():
        if len(indices) == 1:
            hidden_indices.add(indices[0])
    lines = [line for index, line in enumerate(all_lines)
             if index not in hidden_indices]
    hidden = len(hidden_indices)
    hidden_note = (f" {hidden} known harmless message"
                   f"{'s' if hidden != 1 else ''} hidden." if hidden else "")
    if not lines:
        return result("log", title, "ok",
                      f"No unusual error messages since you started up.{hidden_note}")
    count_text = ("One unusual error message since you started up"
                  if len(lines) == 1
                  else f"{len(lines)} unusual error messages since you started up")
    return result(
        "log", title, "info",
        f"{count_text}.{hidden_note}",
        "These messages may be harmless. If something is not working, the "
        "details below can help explain why.",
        detail="\n".join(lines[-10:])[:1500])


def check_drive():
    """Drive health. Asks for the admin password, so it runs only on request."""
    title = "Drive health"
    if not shutil.which("smartctl"):
        return result("drive", title, "info",
                      "The drive health tool is not installed.")
    rc, listing = run([
        "lsblk", "--json", "--nodeps", "--output", "NAME,TYPE,TRAN,RM"])
    if rc != 0:
        return result("drive", title, "info",
                      "Could not list the computer's internal drives.",
                      detail=listing.strip()[:1500])
    drives = internal_drives(listing)
    if drives is None:
        return result("drive", title, "info",
                      "Could not read the computer's internal drive list.",
                      detail=listing.strip()[:1500])
    if not drives:
        return result("drive", title, "info",
                      "No internal drives were found to check.")

    reports = []
    levels = []
    for device in drives:
        rc, output = run(
            ["pkexec", "smartctl", "-a", f"/dev/{device}"], timeout=120)
        if rc == 126:
            return result("drive", title, "info",
                          "The password request was cancelled.",
                          "You can try the drive check again whenever you like.")
        if rc == 127:
            reports.append(f"{device}: could not start the drive check.")
            levels.append("info")
            continue
        if rc == 124:
            reports.append(f"{device}: the drive check took too long.")
            levels.append("info")
            continue
        if not output.strip():
            reports.append(f"{device}: no health report was returned.")
            levels.append("info")
            continue
        if re.search(r"does not support SMART|SMART support is:\s*Unavailable",
                     output, re.IGNORECASE):
            reports.append(f"{device}: this drive does not support SMART.")
            levels.append("info")
            continue

        health = re.search(
            r"(?:overall-health self-assessment test result|SMART Health Status)"
            r":\s*(\w+)", output, re.IGNORECASE)
        crit = re.search(r"Critical Warning:\s*(0x[0-9a-fA-F]+)", output)
        used = re.search(r"Percentage Used:\s*(\d+)%", output)
        errs = re.search(r"Media and Data Integrity Errors:\s*(\d+)", output)
        if not (health or crit):
            reports.append(f"{device}: could not read a SMART health result.")
            levels.append("info")
            continue
        status = health.group(1) if health else "unknown"
        critical = crit.group(1) if crit else "unknown"
        percent_used = int(used.group(1)) if used else None
        errors = int(errs.group(1)) if errs else 0
        reports.append(
            f"{device}: health {status}; critical warning {critical}; "
            f"lifespan used {percent_used if percent_used is not None else 'unknown'}%; "
            f"data errors {errors}.")
        if (status.upper() not in ("PASSED", "OK", "UNKNOWN")
                or errors > 0
                or critical.lower() not in ("0x00", "unknown")):
            levels.append("bad")
        elif rc != 0:
            levels.append("info")
        elif percent_used is not None and percent_used >= 80:
            levels.append("warn")
        else:
            levels.append("ok")

    detail = "\n".join(reports)
    if "bad" in levels:
        return result("drive", title, "bad",
                      "At least one drive is reporting a health problem.",
                      "Back up important files now and consider having the drive checked.",
                      detail=detail)
    if "warn" in levels:
        return result("drive", title, "warn",
                      "At least one drive has used much of its expected lifespan.",
                      "Back up important files and keep an eye on the drive.",
                      detail=detail)
    if "info" in levels:
        return result("drive", title, "info",
                      "Some drives could not be checked for SMART health.",
                      "Some drives do not support SMART; others may not provide a readable report.",
                      detail=detail)
    return result("drive", title, "ok",
                  f"All {len(drives)} internal drives look healthy.",
                  detail=detail)


CHECKS = [check_extensions, check_crashes, check_hardware, check_updates,
          check_packages, check_disk, check_memory, check_services,
          check_log_errors]


def run_all():
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except (ArithmeticError, AttributeError, IndexError, KeyError, OSError,
                TypeError, ValueError) as exc:
            out.append(result(fn.__name__, fn.__name__.replace("check_", "").title(),
                              "info", "This check could not run.", detail=str(exc)))
    return out


def overall(results):
    levels = [r["level"] for r in results]
    if "bad" in levels:
        return "bad"
    if "warn" in levels:
        return "warn"
    return "ok"


# ------------------------------------------------------------------ state

def save_state(results):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"time": datetime.datetime.now().isoformat(),
                       "status": overall(results)}, f)
    except OSError as exc:
        print(_("Desktop Checkup could not save its last-run date: {error}").format(
            error=exc), file=sys.stderr)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# -------------------------------------------------------------------- CLI

def cli():
    res = sorted(run_all(), key=lambda r: ORDER[r["level"]])
    for r in res:
        print(f"[{r['level'].upper():4}] {r['title']}: {r['summary']}")
        if r["help"] and r["level"] in ("warn", "bad"):
            print(f"       {r['help']}")
        for action in r["fix"]:
            print(_("       Possible fix: {description}").format(
                description=action["description"]))
    save_state(res)


# ---------------------------------------------------------- install/remind

def desktop_paths():
    home = os.path.expanduser("~")
    return (os.path.join(home, ".local/share/applications",
                         "io.github.dariocostanzo.DesktopCheckup.desktop"),
            os.path.join(home, ".config/autostart/desktop-checkup-reminder.desktop"))


def package_installed(package):
    rc, out = run(["dpkg-query", "-s", package])
    return rc == 0 and "Status: install ok installed" in out


def desktop_quote(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return f'"{escaped}"'


def install():
    missing = [package for package in GUI_PACKAGES if not package_installed(package)]
    if missing:
        packages = " ".join(missing)
        print(_("Desktop Checkup needs these system packages before it can open:"))
        print(f"  {packages}")
        print(_("Install them with: sudo apt install {packages}").format(
            packages=packages))
        return 1
    app_entry, auto_entry = desktop_paths()
    os.makedirs(os.path.dirname(app_entry), exist_ok=True)
    os.makedirs(os.path.dirname(auto_entry), exist_ok=True)
    executable = desktop_quote(sys.executable)
    script = desktop_quote(os.path.realpath(__file__))
    with open(app_entry, "w") as f:
        f.write(("[Desktop Entry]\nType=Application\nName={}\n"
                 "Comment={}\nExec={} {}\n"
                 "Icon=utilities-system-monitor\nTerminal=false\n"
                 "Categories=System;Monitor;\n").format(
                     _("Desktop Checkup"),
                     _("Check how healthy your computer is"),
                     executable, script))
    with open(auto_entry, "w") as f:
        f.write(("[Desktop Entry]\nType=Application\nName={}\n"
                 "Exec={} {} --daily\nNoDisplay=true\n"
                 "X-GNOME-Autostart-enabled=true\n").format(
                     _("Desktop Checkup daily check"), executable, script))
    print(_("Installed. Find 'Desktop Checkup' in your apps menu."))
    print(_("A quick check will run when you sign in and let you know if it finds a problem."))
    return 0


def uninstall():
    paths = (*desktop_paths(), STATE_FILE)
    for p in paths:
        if os.path.exists(p):
            os.remove(p)
    try:
        os.rmdir(DATA_DIR)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(_("Could not remove the Desktop Checkup data folder: {error}").format(
            error=exc), file=sys.stderr)
    print(_("Removed the app menu entry, sign-in check, and Desktop Checkup files."))
    if os.path.isdir(DATA_DIR) and os.listdir(DATA_DIR):
        print(_("Other files in {path} were left untouched.").format(
            path=DATA_DIR))


def notify(title, message):
    if not shutil.which("notify-send"):
        return 127, _("The desktop notification tool is not installed.")
    return run(["notify-send", "--app-name=Desktop Checkup", title, message])


def remind():
    st = load_state()
    due = True
    if st:
        try:
            last = datetime.datetime.fromisoformat(st["time"])
            due = (datetime.datetime.now() - last).days >= REMIND_DAYS
        except (KeyError, ValueError):
            due = True
    if due:
        rc, message = notify(_("Desktop Checkup"),
                             _("It has been a while since your last check. "
                               "Open Desktop Checkup from your apps menu."))
        if rc != 0:
            print(_("Desktop Checkup could not show the reminder: {message}").format(
                message=message), file=sys.stderr)


def daily():
    results = run_all()
    save_state(results)
    problems = [r for r in results if r["level"] in ("warn", "bad")]
    if problems:
        summary = _("A few things may need your attention: {items}").format(
            items="; ".join(f"{r['title']}: {r['summary']}" for r in problems[:3]))
        if len(problems) > 3:
            summary += _("; and more.")
        rc, message = notify(_("Desktop Checkup found a problem"), summary)
        if rc != 0:
            print(_("Desktop Checkup could not show a notification: {message}").format(
                message=message), file=sys.stderr)
    return 0


def test_notification():
    rc, message = notify(_("Desktop Checkup test"),
                         _("Notifications are working on this computer."))
    if rc == 0:
        print(_("Test notification sent."))
        return 0
    print(_("Could not send the test notification: {message}").format(
        message=message or _("the desktop reported an error.")), file=sys.stderr)
    return 1


def command_argv(action, step):
    return (["pkexec"] if action["needs_root"] else []) + list(step)


def display_fix_commands(action):
    return _("\nThen: ").join(shlex.join(command_argv(action, step))
                           for step in action["steps"])


def run_fix(action):
    output = []
    failed_rc = None
    for step in action["steps"]:
        argv = command_argv(action, step)
        try:
            completed = subprocess.run(argv, capture_output=True, text=True,
                                       timeout=900, env=ENV, shell=False,
                                       check=False)
            rc = completed.returncode
            text = (completed.stdout or "") + (completed.stderr or "")
        except OSError as exc:
            rc, text = 127, str(exc)
        except subprocess.TimeoutExpired as exc:
            rc = 124
            parts = (exc.stdout, exc.stderr)
            text = "".join(
                part.decode(errors="replace") if isinstance(part, bytes)
                else part or ""
                for part in parts)
            text += _("\nThe command took too long and was stopped.")
        output.append(text.strip())
        if rc != 0:
            if rc in (124, 126, 127) or failed_rc is None:
                failed_rc = rc
            joined = "\n".join(part for part in output if part)
            if rc in (124, 126, 127) or re.search(
                    r"could not get lock|unable to acquire.*lock|"
                    r"could not open lock file|is another process using it",
                    joined, re.IGNORECASE):
                break
    joined = "\n".join(part for part in output if part)
    if failed_rc is None:
        return True, _("The fix worked."), joined
    if failed_rc == 126:
        message = _("The password request was cancelled. No changes were made.")
    elif re.search(r"could not get lock|unable to acquire.*lock|"
                   r"could not open lock file|is another process using it",
                   joined, re.IGNORECASE):
        message = _("Another app is updating your computer. Please try again later.")
    elif failed_rc == 124:
        message = _("The fix took too long and was stopped.")
    else:
        message = _("The fix did not work. See the command output below.")
    return False, message, joined or _(
        "The command stopped with exit code {code}.").format(code=failed_rc)


# -------------------------------------------------------------------- GUI

def run_gui():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, GLib, Gtk, Pango
    except (ImportError, ValueError) as exc:
        packages = " ".join(GUI_PACKAGES)
        print(_("Desktop Checkup needs GTK 4 and libadwaita to open its window."),
              file=sys.stderr)
        print(_("Install them with: sudo apt install {packages}").format(
            packages=packages), file=sys.stderr)
        print(_("Details: {error}").format(error=exc), file=sys.stderr)
        return 1

    ICONS = {"ok": ("object-select-symbolic", "success"),
             "info": ("dialog-information-symbolic", "dim-label"),
             "warn": ("dialog-warning-symbolic", "warning"),
             "bad": ("dialog-error-symbolic", "error")}
    BANNER = {
        "ok": (_("Everything looks good"),
               _("Nothing needs your attention. Check again in about a month.")),
        "warn": (_("A few things are worth a look"),
                 _("Open the orange items below to see what they mean and what you can do.")),
        "bad": (_("Something needs your attention"),
                _("Open the red items below to see what is wrong and what you can do.")),
    }

    class Win(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title=_("Desktop Checkup"),
                             default_width=640, default_height=800)
            self.results = []
            self.drive = None
            self.running = False
            self.fixing = set()
            self.checked_at = None

            self.toasts = Adw.ToastOverlay()
            view = Adw.ToolbarView()
            header = Adw.HeaderBar()
            self.again = Gtk.Button(label=_("Check again"))
            self.again.add_css_class("suggested-action")
            self.again.connect("clicked", lambda _b: self.start())
            header.pack_start(self.again)
            view.add_top_bar(header)

            scroll = Gtk.ScrolledWindow(vexpand=True)
            clamp = Adw.Clamp(maximum_size=640, margin_top=12, margin_bottom=24,
                              margin_start=12, margin_end=12)
            self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            clamp.set_child(self.content)
            scroll.set_child(clamp)
            view.set_content(scroll)
            self.toasts.set_child(view)
            self.set_content(self.toasts)
            self.start()

        # -- helpers
        def clear(self):
            child = self.content.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.content.remove(child)
                child = nxt

        def banner(self, icon, css, title, subtitle, spinner=False):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          margin_top=12, halign=Gtk.Align.CENTER)
            if spinner:
                sp = Gtk.Spinner(spinning=True, width_request=48, height_request=48)
                box.append(sp)
            else:
                img = Gtk.Image.new_from_icon_name(icon)
                img.set_pixel_size(64)
                img.add_css_class(css)
                box.append(img)
            t = Gtk.Label(label=title)
            t.add_css_class("title-1")
            s = Gtk.Label(label=subtitle, wrap=True, justify=Gtk.Justification.CENTER)
            s.add_css_class("dim-label")
            box.append(t)
            box.append(s)
            return box

        def make_row(self, r, suffix=None):
            esc = GLib.markup_escape_text
            icon, css = ICONS[r["level"]]
            img = Gtk.Image.new_from_icon_name(icon)
            img.add_css_class(css)
            has_more = r["help"] or r["fix"] or r["detail"]
            if not has_more:
                row = Adw.ActionRow(title=esc(r["title"]), subtitle=esc(r["summary"]))
                row.add_prefix(img)
                if suffix:
                    row.add_suffix(suffix)
                return row
            row = Adw.ExpanderRow(title=esc(r["title"]), subtitle=esc(r["summary"]))
            row.add_prefix(img)
            if suffix:
                row.add_suffix(suffix)
            if r["help"]:
                row.add_row(Adw.ActionRow(title=_("What this means"), subtitle=esc(r["help"])))
            for action in r["fix"]:
                action_row = Adw.ActionRow(title=_("Available fix"),
                                           subtitle=esc(action["description"]))
                button = Gtk.Button(label=action["label"], valign=Gtk.Align.CENTER,
                                    tooltip_text=action["description"])
                button.add_css_class("suggested-action")
                button.connect("clicked",
                               lambda btn, check=r["id"], fix_action=action,
                               parent=row:
                               self.confirm_fix(btn, parent, check, fix_action))
                row.add_row(action_row)
                row.add_suffix(button)
            if r["detail"]:
                lab = Gtk.Label(label=_("Details:\n{detail}").format(
                                    detail=r["detail"][:1500]),
                                selectable=True, wrap=True,
                                wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0,
                                margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
                lab.add_css_class("monospace")
                lab.add_css_class("dim-label")
                row.add_row(lab)
            if r["level"] in ("warn", "bad"):
                row.set_expanded(True)
            return row

        def confirm_fix(self, button, action_row, check_id, action):
            command = display_fix_commands(action)
            body = _("{description}\n\nCommand:\n{command}\n\n"
                     "Do you want to go ahead?").format(
                         description=action["description"], command=command)
            alert_type = getattr(Adw, "AlertDialog", None)
            if alert_type is not None:
                dialog = alert_type.new(_("Before we make this change"), body)
                dialog.add_response("cancel", _("Cancel"))
                dialog.add_response("continue", _("Continue"))
                dialog.set_default_response("cancel")
                dialog.set_close_response("cancel")
                dialog.choose(
                    self, None,
                    lambda dlg, response:
                    self.alert_choice(dlg, response, button, action_row,
                                      check_id, action))
                return

            dialog = Adw.MessageDialog.new(
                self, _("Before we make this change"), body)
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("continue", _("Continue"))
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect(
                "response",
                lambda dlg, response:
                self.message_choice(dlg, response, button, action_row,
                                    check_id, action))
            dialog.present()

        def alert_choice(self, dialog, response, button, action_row,
                         check_id, action):
            choice = dialog.choose_finish(response)
            if choice == "continue":
                self.start_fix(button, action_row, check_id, action)
            return False

        def message_choice(self, dialog, response, button, action_row,
                           check_id, action):
            dialog.close()
            if response == "continue":
                self.start_fix(button, action_row, check_id, action)

        def start_fix(self, button, action_row, check_id, action):
            if check_id in self.fixing:
                return
            self.fixing.add(check_id)
            self.again.set_sensitive(False)
            button.set_sensitive(False)
            button.set_label(_("Working…"))
            spinner = Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER)
            action_row.add_suffix(spinner)
            check = next((fn for fn in CHECKS
                          if result_id(fn) == check_id), None)
            if check_id == "drive":
                check = check_drive
            if check is None:
                self.fixing.discard(check_id)
                self.again.set_sensitive(True)
                button.set_sensitive(True)
                button.set_label(_("Fix"))
                self.toasts.add_toast(Adw.Toast.new(
                    _("This check cannot be refreshed.")))
                return

            def work():
                succeeded, message, output = run_fix(action)
                try:
                    updated = check()
                except (ArithmeticError, AttributeError, IndexError, KeyError,
                        OSError, TypeError, ValueError) as exc:
                    updated = result(check_id, _("Health check"), "info",
                                     _("Could not refresh this check."), detail=str(exc))
                if not succeeded:
                    previous = updated["detail"]
                    failure = _("Fix result: {message}\n\nCommand output:\n{output}").format(
                        message=message, output=output)
                    updated["detail"] = "\n\n".join(
                        part for part in (previous, failure) if part)
                GLib.idle_add(self.fix_done, check_id, updated, succeeded, message)
            threading.Thread(target=work, daemon=True).start()

        def fix_done(self, check_id, updated, succeeded, message):
            self.fixing.discard(check_id)
            self.again.set_sensitive(not self.running and not self.fixing)
            if check_id == "drive":
                self.drive = updated
            else:
                for index, old in enumerate(self.results):
                    if old["id"] == check_id:
                        self.results[index] = updated
                        break
            if succeeded:
                self.toasts.add_toast(Adw.Toast.new(_("The fix worked.")))
            else:
                self.toasts.add_toast(Adw.Toast.new(message))
            self.checked_at = datetime.datetime.now()
            save_state(self.results)
            self.render()
            return False

        # -- running
        def start(self):
            if self.running or self.fixing:
                return
            self.running = True
            self.again.set_sensitive(False)
            self.clear()
            self.content.append(self.banner(
                "", "", _("Checking your computer"),
                _("This takes a few seconds."), spinner=True))

            def work():
                res = run_all()
                GLib.idle_add(self.done, res)
            threading.Thread(target=work, daemon=True).start()

        def done(self, res):
            self.results = res
            self.running = False
            self.again.set_sensitive(True)
            self.checked_at = datetime.datetime.now()
            save_state(res)
            self.render()
            return False

        def run_drive(self, btn):
            btn.set_sensitive(False)
            btn.set_label(_("Checking..."))

            def work():
                r = check_drive()
                GLib.idle_add(self.drive_done, r)
            threading.Thread(target=work, daemon=True).start()

        def drive_done(self, r):
            self.drive = r
            self.render()
            return False

        def render(self):
            self.clear()
            status = overall(self.results + ([self.drive] if self.drive else []))
            title, sub = BANNER[status]
            icon, css = ICONS[status]
            self.content.append(self.banner(icon, css, title, sub))

            when = self.checked_at.strftime("%d %B %Y, %H:%M") if self.checked_at else ""
            stamp = Gtk.Label(
                label=_("Checked on {when}").format(when=when) if when else "")
            stamp.add_css_class("dim-label")
            self.content.append(stamp)

            group = Adw.PreferencesGroup(title=_("Your computer"))
            for r in sorted(self.results, key=lambda x: ORDER[x["level"]]):
                group.add(self.make_row(r))
            self.content.append(group)

            dgroup = Adw.PreferencesGroup(
                title=_("Drive health"),
                description=_("Looks for signs of wear. You will be asked for your password."))
            btn = Gtk.Button(label=_("Check drive") if not self.drive else _("Check again"),
                             valign=Gtk.Align.CENTER)
            btn.connect("clicked", self.run_drive)
            if self.drive:
                dgroup.add(self.make_row(self.drive, btn))
            else:
                row = Adw.ActionRow(title=_("Not checked yet"),
                                    subtitle=_("Choose Check drive to see its health report."))
                row.add_suffix(btn)
                dgroup.add(row)
            self.content.append(dgroup)

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID)

        def do_activate(self):
            win = self.props.active_window or Win(self)
            win.present()

    return App().run(sys.argv[:1])


def result_id(check_function):
    return {
        check_extensions: "ext",
        check_crashes: "crash",
        check_hardware: "hw",
        check_updates: "upd",
        check_packages: "pkg",
        check_disk: "disk",
        check_memory: "mem",
        check_services: "svc",
        check_log_errors: "log",
    }.get(check_function)


def main():
    args = sys.argv[1:]
    if "--cli" in args:
        cli()
    elif "--install" in args:
        return install()
    elif "--uninstall" in args:
        uninstall()
        return 0
    elif "--remind" in args:
        remind()
        return 0
    elif "--daily" in args:
        return daily()
    elif "--test-notification" in args:
        return test_notification()
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
