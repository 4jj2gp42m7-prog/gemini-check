"""Offline regression tests for one-command browser setup.

No test is allowed to run apt, pip, or contact Google. Browser verification itself
is covered separately by test_browser.py using intercepted Chromium pages.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "gemini-check.sh"


def script_namespace():
    source = SCRIPT.read_text(encoding="utf-8")
    source = source.split("<<'PYTHON_GEMINI_CHECK'\n", 1)[1].rsplit("\nPYTHON_GEMINI_CHECK", 1)[0]
    namespace = {"__name__": "gemini_auto_test", "__file__": str(SCRIPT)}
    exec(compile(source, str(SCRIPT) + ":python", "exec"), namespace)
    return namespace


def successful_browser():
    return {"status": "working", "detail": "new local fixture response",
            "ip": "203.0.113.77", "country": "DE", "colo": "FRA", "warp": "off",
            "submitted": True, "response_verified": True, "request_seen": True}


class AutoArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()

    def arguments(self, *args):
        with patch.object(sys, "argv", ["gemini-check", *args]):
            return self.namespace["arguments"]()

    def test_auto_implies_browser_without_api_or_key_validation(self):
        with patch.dict(os.environ, {"GEMINI_KEY": "invalid\nignored API key", "GEMINI_PROXY": ""}):
            args, key = self.arguments("--auto", "--json")
        self.assertTrue(args.auto)
        self.assertTrue(args.browser)
        self.assertEqual(key, "")

    def test_auto_accepts_browser_report_and_proxy_options(self):
        args, key = self.arguments("--auto", "--proxy", "socks5h://127.0.0.1:40000",
                                   "--json", "--browser-timeout", "90",
                                   "--save", "/tmp/future-auto-report.json",
                                   "--compare", "/tmp/previous-auto-report.json")
        self.assertTrue(args.browser)
        self.assertEqual(args.proxy, "socks5h://127.0.0.1:40000")
        self.assertEqual(args.browser_timeout, 90)
        self.assertTrue(args.json)
        self.assertEqual(key, "")

    def test_auto_rejects_incompatible_or_api_options(self):
        for options in (("-4",), ("-6",), ("--both",), ("--install-browser",),
                        ("--model", "gemini-flash-lite-latest"),
                        ("--key-file", "/tmp/should-never-be-read.key")):
            with self.subTest(options=options):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    self.arguments("--auto", *options, "--json")
                self.assertEqual(raised.exception.code, 64)


class AutoMainTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()

    def test_setup_finishes_before_browser_and_never_calls_api(self):
        calls = []

        def prepare(args):
            calls.append("prepare")
            self.assertTrue(args.auto)
            self.assertTrue(args.browser)
            return 0

        def browser(**kwargs):
            calls.append("browser")
            self.assertEqual(calls[0], "prepare")
            return successful_browser()

        def forbidden(*args, **kwargs):
            self.fail("--auto called the curl/API diagnostics")

        captured = io.StringIO()
        with patch.dict(self.namespace, {"prepare_auto_browser": prepare,
                                         "reexec_auto_browser": lambda: None,
                                         "probe_browser": browser, "check_route": forbidden}), \
                patch.dict(os.environ, {"GEMINI_KEY": "not even a valid\nkey", "GEMINI_PROXY": ""}), \
                patch.object(sys, "argv", ["gemini-check", "--auto", "--json"]), \
                contextlib.redirect_stdout(captured):
            code = self.namespace["main"]()
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["prepare", "browser"])
        report = json.loads(captured.getvalue())
        self.assertEqual(len(report["results"]), 1)
        route = report["results"][0]
        self.assertEqual(route["id"], "browser")
        self.assertEqual(route["api"]["status"], "not_checked")
        self.assertEqual(route["assessment"]["status"], "web_confirmed")

    def test_failed_setup_does_not_run_browser_or_report_an_ip_ban(self):
        def failed(args):
            print("fixture: installation failed", file=sys.stderr)
            return 2

        def forbidden(*args, **kwargs):
            self.fail("browser ran despite unsuccessful setup")

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(self.namespace, {"prepare_auto_browser": failed, "probe_browser": forbidden}), \
                patch.object(sys, "argv", ["gemini-check", "--auto", "--json"]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.namespace["main"]()
        self.assertEqual(code, 2)
        self.assertIn("installation failed", stderr.getvalue())
        self.assertNotIn("restricted", stdout.getvalue())
        self.assertNotIn("web_confirmed", stdout.getvalue())

    def test_plain_browser_does_not_implicitly_install_packages(self):
        def forbidden(*args, **kwargs):
            self.fail("ordinary --browser tried to install packages")

        captured = io.StringIO()
        with patch.dict(self.namespace, {"prepare_auto_browser": forbidden,
                                         "probe_browser": lambda **kwargs: successful_browser()}), \
                patch.object(sys, "argv", ["gemini-check", "--browser", "--json"]), \
                contextlib.redirect_stdout(captured):
            code = self.namespace["main"]()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(captured.getvalue())["results"][0]["browser"]["status"], "working")

    def test_json_remains_a_single_report_when_setup_has_progress_output(self):
        readiness = iter((False, True))

        def install(*, with_system_deps=False, output=None):
            self.assertIs(output, sys.stderr)
            print("fixture installation progress", file=output)
            return 0

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(self.namespace, {"auto_browser_ready": lambda: next(readiness),
                                         "install_browser": install,
                                         "reexec_auto_browser": lambda: None,
                                         "probe_browser": lambda **kwargs: successful_browser()}), \
                patch.object(sys, "argv", ["gemini-check", "--auto", "--json"]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.namespace["main"]()
        self.assertEqual(code, 0)
        self.assertIn("fixture installation progress", stderr.getvalue())
        self.assertNotIn("fixture installation progress", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["results"][0]["assessment"]["status"], "web_confirmed")


class AutoPreparationTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()

    def arguments(self):
        with patch.object(sys, "argv", ["gemini-check", "--auto", "--json"]):
            return self.namespace["arguments"]()[0]

    def prepare(self):
        # All expected installation calls go through the explicitly mocked helper.
        # An accidental real apt/pip invocation must fail the test before execution.
        with patch.object(self.namespace["subprocess"], "run", side_effect=AssertionError("unexpected external installation command")):
            return self.namespace["prepare_auto_browser"](self.arguments())

    def test_healthy_runtime_is_reused_without_installing_packages(self):
        def forbidden(*args, **kwargs):
            self.fail("healthy cached runtime was reinstalled")
        with patch.dict(self.namespace, {"auto_browser_ready": lambda: True,
                                         "install_browser": forbidden}):
            self.assertEqual(self.prepare(), 0)

    def test_linux_missing_runtime_installs_system_dependencies_once(self):
        readiness = iter((False, True))
        calls = []
        def install(**kwargs):
            calls.append(kwargs)
            return 0
        with patch.dict(self.namespace, {"auto_browser_ready": lambda: next(readiness),
                                         "install_browser": install}), \
                patch.object(sys, "platform", "linux"):
            self.assertEqual(self.prepare(), 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["with_system_deps"])
        self.assertIs(calls[0]["output"], sys.stderr)

    def test_macos_does_not_request_linux_package_dependencies(self):
        readiness = iter((False, True))
        calls = []
        with patch.dict(self.namespace, {"auto_browser_ready": lambda: next(readiness),
                                         "install_browser": lambda **kwargs: calls.append(kwargs) or 0}), \
                patch.object(sys, "platform", "darwin"):
            self.assertEqual(self.prepare(), 0)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["with_system_deps"])

    def test_installer_failure_is_returned_without_retry_loop(self):
        calls = []
        with patch.dict(self.namespace, {"auto_browser_ready": lambda: False,
                                         "install_browser": lambda **kwargs: calls.append(kwargs) or 2}):
            self.assertEqual(self.prepare(), 2)
        self.assertEqual(len(calls), 1)

    def test_successful_install_is_not_enough_if_launch_still_fails(self):
        calls = []
        with patch.dict(self.namespace, {"auto_browser_ready": lambda: False,
                                         "install_browser": lambda **kwargs: calls.append(kwargs) or 0}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.prepare(), 2)
        self.assertEqual(len(calls), 1)


class AutoReexecTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()
        self.temp = tempfile.TemporaryDirectory(prefix="gemini-auto-reexec-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.managed = self.home / "browser-venv"
        (self.managed / "bin").mkdir(parents=True)
        self.executable = self.managed / "bin" / "python"
        self.executable.write_text("fixture executable")
        self.executable.chmod(0o755)
        self.payload = self.home / "payload.py"
        self.payload.write_text("# fixture payload\n")
        self.namespace["__file__"] = str(self.payload)
        self.namespace["browser_home"] = lambda: self.home

    def test_reexec_preserves_requested_proxy_without_exposing_it_in_argv(self):
        proxy = "socks5h://private-user:private-password@127.0.0.1:40000"
        calls = []
        class Reexecuted(Exception):
            pass
        def execve(executable, argv, environment):
            calls.append((executable, argv, environment))
            raise Reexecuted
        with patch.object(os, "execve", execve), \
                patch.object(sys, "prefix", str(self.home / "system-python")), \
                patch.object(sys, "argv", [str(self.payload), "--auto", "--json"]), \
                patch.dict(os.environ, {"GEMINI_KEY": "never-pass-this-key", "GEMINI_PROXY": proxy,
                                        "GEMINI_BROWSER_HOME": str(self.home)}), \
                self.assertRaises(Reexecuted):
            self.namespace["reexec_auto_browser"]()
        self.assertEqual(len(calls), 1)
        executable, argv, environment = calls[0]
        self.assertEqual(Path(executable), self.executable)
        self.assertEqual([str(x) for x in argv[:2]], [str(self.executable), str(self.payload)])
        self.assertIn("--auto", argv)
        self.assertEqual(environment["GEMINI_PROXY"], proxy)
        self.assertEqual(environment["GEMINI_BROWSER_HOME"], str(self.home))
        self.assertEqual(environment["GEMINI_AUTO_REEXEC"], "1")
        self.assertNotIn("GEMINI_KEY", environment)
        self.assertNotIn(proxy, " ".join(str(x) for x in argv))

    def test_current_managed_runtime_does_not_reexec_again(self):
        with patch.object(os, "execve", side_effect=AssertionError("managed runtime recursively reexeced")), \
                patch.object(sys, "prefix", str(self.managed)), \
                patch.dict(os.environ, {"GEMINI_AUTO_REEXEC": "1"}):
            self.namespace["reexec_auto_browser"]()

    def test_failed_runtime_transition_cannot_reexec_indefinitely(self):
        with patch.object(os, "execve", side_effect=AssertionError("reexec loop was not stopped")), \
                patch.object(sys, "prefix", str(self.home / "wrong-base-python")), \
                patch.dict(os.environ, {"GEMINI_AUTO_REEXEC": "1"}), \
                self.assertRaises(OSError):
            self.namespace["reexec_auto_browser"]()


class AutoInstallerPipelineTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()
        self.temp = tempfile.TemporaryDirectory(prefix="gemini installer with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.executable = self.home / "browser-venv" / "bin" / "python"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text("fixture managed Python")
        self.executable.chmod(0o755)
        self.namespace["browser_home"] = lambda: self.home

    def test_linux_auto_install_requires_privileges_and_routes_progress_to_stderr(self):
        calls, privileges = [], []
        output = io.StringIO()

        def privilege():
            privileges.append(True)
            return ["sudo", "-n"]

        def run(command, **kwargs):
            calls.append((command, kwargs))
            self.assertIs(kwargs["stdout"], output)
            self.assertNotIn("GEMINI_KEY", kwargs["env"])
            self.assertNotIn("GEMINI_PROXY", kwargs["env"])
            self.assertEqual(kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive")
            self.assertEqual(kwargs["env"]["NEEDRESTART_MODE"], "l")
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(self.namespace, {"_auto_linux_privilege": privilege,
                                         "_python_supports": lambda *args, **kwargs: True,
                                         "_find_browser_python": lambda **kwargs: "/fixture/external-python"}), \
                patch.object(subprocess, "run", run), patch.object(sys, "platform", "linux"), \
                patch.dict(os.environ, {"GEMINI_KEY": "PRIVATE-test-key", "GEMINI_PROXY": "http://private:password@127.0.0.1:3128"}), \
                contextlib.redirect_stderr(output):
            code = self.namespace["install_browser"](with_system_deps=True, output=sys.stderr)
        self.assertEqual(code, 0)
        self.assertEqual(privileges, [True])
        dependency_calls = [command for command, _ in calls if "install-deps" in command]
        self.assertEqual(len(dependency_calls), 1)
        self.assertEqual(dependency_calls[0][:2], ["sudo", "-n"])
        self.assertEqual(dependency_calls[0][-4:], ["-m", "playwright", "install-deps", "chromium"])
        self.assertTrue(any(command[-4:] == ["-m", "playwright", "install", "chromium"] for command, _ in calls))
        self.assertNotIn("PRIVATE-test-key", output.getvalue())

        def no_privilege():
            raise RuntimeError("fixture: root privileges are required")
        with patch.dict(self.namespace, {"_auto_linux_privilege": no_privilege}), \
                patch.object(subprocess, "run", side_effect=AssertionError("installation ran without required privileges")), \
                patch.object(sys, "platform", "linux"), contextlib.redirect_stderr(output):
            self.assertEqual(self.namespace["install_browser"](with_system_deps=True, output=output), 2)
        self.assertIn("root privileges are required", output.getvalue())

    def test_manual_install_never_invokes_system_package_installation(self):
        calls = []
        output = io.StringIO()

        def run(command, **kwargs):
            calls.append(command)
            self.assertIs(kwargs["stdout"], output)
            self.assertNotIn("GEMINI_KEY", kwargs["env"])
            self.assertNotIn("GEMINI_PROXY", kwargs["env"])
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(self.namespace, {"_python_supports": lambda *args, **kwargs: True,
                                         "_auto_linux_privilege": lambda: self.fail("manual mode requested system privileges")}), \
                patch.object(subprocess, "run", run), patch.object(sys, "platform", "linux"), \
                patch.dict(os.environ, {"GEMINI_KEY": "PRIVATE-test-key", "GEMINI_PROXY": "socks5h://private:password@127.0.0.1:1080"}):
            self.assertEqual(self.namespace["install_browser"](with_system_deps=False, output=output), 0)
        self.assertGreaterEqual(len(calls), 1)
        for command in calls:
            self.assertEqual(Path(command[0]), self.executable)
            self.assertNotIn("apt-get", command)
            self.assertNotIn("install-deps", command)
            self.assertNotIn("sudo", command)
        self.assertTrue(any(command[-4:] == ["-m", "playwright", "install", "chromium"] for command in calls))

    def test_unisolated_replacement_venv_is_rejected_before_any_package_install(self):
        external = self.home / "external-python"
        external.write_text("fixture external Python")
        calls = []
        output = io.StringIO()
        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)
        with patch.dict(self.namespace, {"_python_supports": lambda *args, **kwargs: False,
                                         "_find_browser_python": lambda **kwargs: str(external)}), \
                patch.object(subprocess, "run", run), contextlib.redirect_stderr(output):
            result = self.namespace["install_browser"](with_system_deps=False, output=output)
        self.assertEqual(result, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:3], ["-m", "venv"])
        self.assertFalse(any("ensurepip" in command or "pip" in command or "playwright" in command for command in calls))
        backups = list(self.home.glob("browser-venv.previous-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "bin" / "python").read_text(), "fixture managed Python")


@unittest.skipIf(sys.version_info < (3, 10), "venv prefix regression needs Python 3.10+")
class AutoManagedPrefixTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()
        self.temp = tempfile.TemporaryDirectory(prefix="gemini managed prefix ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.managed = self.home / "browser-venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.managed)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.executable = self.managed / "bin" / "python"

    def test_correct_managed_prefix_is_required_in_addition_to_python_version(self):
        supports = self.namespace["_python_supports"]
        self.assertTrue(supports(self.executable, expected_prefix=self.managed))
        self.assertFalse(supports(self.executable, expected_prefix=self.home / "another-venv"))

    def test_missing_pyvenv_cfg_does_not_turn_base_python_into_a_managed_runtime(self):
        (self.managed / "pyvenv.cfg").unlink()
        supports = self.namespace["_python_supports"]
        self.assertTrue(supports(self.executable), "fixture must still execute a supported base Python")
        self.assertFalse(supports(self.executable, expected_prefix=self.managed))

    def test_readiness_rejects_base_python_before_launching_even_a_usable_browser(self):
        (self.managed / "pyvenv.cfg").unlink()
        site = self.home / "fake-site"
        package = site / "playwright"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        marker = self.home / "browser-was-launched"
        (package / "sync_api.py").write_text("""import os
from pathlib import Path
class Browser:
    def close(self):
        pass
class Chromium:
    def launch(self, **kwargs):
        Path(os.environ['GEMINI_TEST_BROWSER_MARKER']).touch()
        return Browser()
class Manager:
    chromium = Chromium()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
def sync_playwright():
    return Manager()
""")
        version = self.namespace["BROWSER_PLAYWRIGHT_VERSION"]
        metadata = site / ("playwright-" + version + ".dist-info")
        metadata.mkdir()
        (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: playwright\nVersion: " + version + "\n")
        environment = {"PYTHONPATH": str(site), "GEMINI_TEST_BROWSER_MARKER": str(marker)}
        self.namespace["browser_home"] = lambda: self.home
        with patch.dict(os.environ, environment):
            # Validate the fake browser fixture without requiring a real browser.
            subprocess.run([str(self.executable), "-c",
                            "from playwright.sync_api import sync_playwright; p=sync_playwright(); p.chromium.launch().close()"],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertTrue(marker.is_file())
            marker.unlink()
            self.assertFalse(self.namespace["auto_browser_ready"]())
        self.assertFalse(marker.exists(), "a base Python must be rejected before browser launch")


class AutoReadinessTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()
        self.temp = tempfile.TemporaryDirectory(prefix="gemini-auto-ready-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.executable = self.home / "browser-venv" / "bin" / "python"
        self.namespace["browser_home"] = lambda: self.home

    def make_runtime(self):
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text("fixture executable")
        self.executable.chmod(0o755)

    def test_missing_managed_python_is_not_ready_and_does_not_install(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("missing runtime executed an external command")):
            self.assertFalse(self.namespace["auto_browser_ready"]())

    def test_successful_health_check_uses_only_the_managed_python(self):
        self.make_runtime()
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        with patch.object(subprocess, "run", run):
            self.assertTrue(self.namespace["auto_browser_ready"]())
        self.assertGreaterEqual(len(calls), 1)
        for command, _ in calls:
            self.assertEqual(Path(command[0]), self.executable)
            self.assertNotIn("pip", command)
            self.assertNotIn("apt-get", command)

    def test_failed_health_check_is_not_ready(self):
        self.make_runtime()
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="")):
            self.assertFalse(self.namespace["auto_browser_ready"]())

    def test_hung_or_broken_health_check_returns_not_ready(self):
        self.make_runtime()
        for error in (OSError("fixture executable is broken"), subprocess.TimeoutExpired([str(self.executable)], 5)):
            with self.subTest(error=type(error).__name__), patch.object(subprocess, "run", side_effect=error):
                self.assertFalse(self.namespace["auto_browser_ready"]())


class AutoShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gemini-auto-shell-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.apt_marker = self.directory / "apt-was-called"
        apt = self.bin / "apt-get"
        apt.write_text("#!/bin/sh\nprintf forbidden > \"$GEMINI_TEST_APT_MARKER\"\nexit 97\n")
        apt.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text("#!/bin/sh\nexit 0\n")
        curl.chmod(0o755)

    def invoke(self, *args):
        environment = dict(os.environ)
        environment.update({"PATH": str(self.bin) + os.pathsep + environment.get("PATH", ""),
                            "GEMINI_BROWSER_HOME": str(self.directory / "browser-home"),
                            "GEMINI_BROWSER_PYTHON": sys.executable,
                            "GEMINI_TEST_APT_MARKER": str(self.apt_marker),
                            "GEMINI_KEY": "invalid ignored\nkey",
                            "TMPDIR": str(self.directory)})
        return subprocess.run(["/bin/bash", str(SCRIPT), *args], env=environment,
                              text=True, capture_output=True, timeout=15)

    def test_auto_help_never_installs_dependencies_and_cleans_payload(self):
        result = self.invoke("--auto", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--auto", result.stdout)
        self.assertFalse(self.apt_marker.exists())
        self.assertFalse(list(self.directory.glob("gemini-check.*")))

    def test_invalid_auto_options_fail_before_any_installation(self):
        for options in (("-4",), ("--model", "gemini-flash-lite-latest"),
                        ("--key-file", str(self.directory / "do-not-read.key"))):
            with self.subTest(options=options):
                result = self.invoke("--auto", *options)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertFalse(self.apt_marker.exists())
                self.assertFalse(list(self.directory.glob("gemini-check.*")))


class AutoBootstrapShellTests(unittest.TestCase):
    """Run the actual pre-Python Bash function with a private fake OS/package manager."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gemini-bootstrap-shell-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.release = self.directory / "os-release"
        self.log = self.directory / "apt.log"
        for name, content in {
            "uname": "#!/bin/sh\nprintf 'Linux\\n'\n",
            "id": "#!/bin/sh\nprintf '0\\n'\n",
            "apt-get": "#!/bin/sh\nprintf '%s|%s|%s\\n' \"${DEBIAN_FRONTEND:-}\" \"${NEEDRESTART_MODE:-}\" \"$*\" >> \"$GEMINI_TEST_APT_LOG\"\nprintf 'fixture apt progress\\n'\nexit \"${GEMINI_TEST_APT_EXIT:-0}\"\n",
        }.items():
            path = self.bin / name
            path.write_text(content)
            path.chmod(0o755)
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("gemini_bootstrap_python() {")
        stop = source.index("\n}\n", start) + len("\n}\n")
        function = source[start:stop].replace("/etc/os-release", str(self.release))
        self.fixture = self.directory / "bootstrap.sh"
        self.fixture.write_text("#!/usr/bin/env bash\nset -eu\n" + function + "\ngemini_bootstrap_python\n")

    def invoke(self, release, apt_exit=0):
        self.release.write_text(release)
        environment = dict(os.environ)
        environment.update({"PATH": str(self.bin) + os.pathsep + environment.get("PATH", ""),
                            "GEMINI_TEST_APT_LOG": str(self.log),
                            "GEMINI_TEST_APT_EXIT": str(apt_exit)})
        return subprocess.run(["/bin/bash", str(self.fixture)], env=environment,
                              text=True, capture_output=True, timeout=10)

    def test_debian_bootstrap_installs_only_prerequisites_with_progress_on_stderr(self):
        result = self.invoke('ID=debian\nVERSION_ID="12"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("fixture apt progress", result.stderr)
        operations = self.log.read_text().splitlines()
        self.assertEqual(len(operations), 2)
        self.assertTrue(operations[0].startswith("noninteractive|l|"))
        self.assertTrue(operations[0].endswith(" update"))
        self.assertIn(" install -y --no-install-recommends ", operations[1])
        for package in ("curl", "python3", "python3-venv", "ca-certificates"):
            self.assertIn(package, operations[1].split())
        self.assertNotIn("upgrade", " ".join(operations).split())

    def test_unsupported_os_never_runs_the_package_manager(self):
        result = self.invoke('ID=debian\nVERSION_ID="11"\n')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(result.stdout, "")

    def test_failed_package_operation_aborts_bootstrap_before_install(self):
        result = self.invoke('ID=debian\nVERSION_ID="12"\n', apt_exit=42)
        self.assertEqual(result.returncode, 2)
        operations = self.log.read_text().splitlines()
        self.assertEqual(len(operations), 1)
        self.assertTrue(operations[0].endswith(" update"))
        self.assertNotIn(" install ", operations[0])


if __name__ == "__main__":
    unittest.main()
