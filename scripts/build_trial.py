#!/usr/bin/env python3
"""Run the fixed, manually dispatched Codex build trial on a hosted Linux runner."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib

GIB = 1024 ** 3
INITIAL_FREE_BYTES = 28 * GIB
MIN_FREE_BYTES = 4 * GIB
CACHE_LIMIT_BYTES = 8 * GIB
SDK_PATHS = ("/usr/local/lib/android", "/opt/ghc", "/usr/share/dotnet")
BINARIES = ("codex", "codex-code-mode-host", "codex-exec-server", "bwrap")


def reject_symlinks(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Symlink is not permitted: {part}")


def assert_hosted_runner(environ):
    if any(environ.get(key) != value for key, value in {
        "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Linux",
    }.items()) or sys.platform != "linux":
        raise RuntimeError("This operation requires a GitHub-hosted Linux runner")
    path = Path(environ.get("RUNNER_TEMP", ""))
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("RUNNER_TEMP must be an existing absolute directory")
    reject_symlinks(path)
    return path


def isolated_environment(work: Path) -> dict[str, str]:
    reject_symlinks(work)
    work.mkdir(parents=True, exist_ok=True)
    paths = {name: work / directory for name, directory in {
        "HOME": "home", "CODEX_HOME": "codex-home", "TMPDIR": "tmp",
        "XDG_CONFIG_HOME": "config", "XDG_CACHE_HOME": "cache",
    }.items()}
    for path in (*paths.values(), work / "cwd"):
        reject_symlinks(path)
        path.mkdir()
    environment = {key: os.environ[key] for key in ("PATH", "RUNNER_TEMP") if key in os.environ}
    environment.update({name: str(path) for name, path in paths.items()})
    environment.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "CI": "true", "GIT_TERMINAL_PROMPT": "0"})

    return environment


def validate_profile(output: str, package_id: str) -> None:
    matches = []
    for line in output.splitlines():
        try:
            artifact = json.loads(line)
        except (ValueError, TypeError) as error:
            raise ValueError("Malformed Cargo JSON artifact stream") from error
        if not isinstance(artifact, dict):
            raise ValueError("Cargo JSON records must be objects")
        target = artifact.get("target", {})
        if (artifact.get("reason") == "compiler-artifact" and artifact.get("package_id") == package_id
                and isinstance(target, dict) and target.get("name") == "codex_tui"
                and isinstance(target.get("kind"), list) and "lib" in target["kind"]):
            matches.append(artifact)
    if len(matches) != 1:
        raise ValueError("Expected exactly one production codex_tui library artifact")
    profile = matches[0].get("profile")
    if not isinstance(profile, dict) or profile.get("test") is not False or profile.get("debug_assertions") is not False:
        raise ValueError("Production TUI artifact must disable tests and debug assertions")


def select_test_names(listing, required) -> list:
    available = [line[:-6] for line in listing.splitlines() if line.endswith(": test")]
    names = []
    for case in required:
        matches = [name for name in available if name == case or name.endswith("::" + case)]
        if len(matches) != 1:
            raise ValueError(f"Required test missing or ambiguous: {case}")
        names.append(matches[0])
    if not names or len(set(names)) != len(names):
        raise ValueError("Required test set must be nonempty and unique")

    return names


def validate_test_run(output, names) -> None:
    counts = re.findall(r"^test result: ok\. (\d+) passed; (\d+) failed; (\d+) ignored;", output, re.MULTILINE)
    if not names or len(set(names)) != len(names) or counts != [(str(len(names)), "0", "0")]:
        raise ValueError("Focused test result count differs from the required set")
    actual = re.findall(r"^test (.+) \.\.\. ok$", output, re.MULTILINE)
    if sorted(actual) != sorted(names):
        raise ValueError("Every required exact test must pass once")


def normalize_lock(text: str, version: str, names: set[str]) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Expected a fixed stable version")
    sections = text.split("[[package]]")
    for index in range(1, len(sections)):
        package = tomllib.loads("[[package]]" + sections[index])["package"][0]
        if package["name"] in names and "source" not in package and package["version"] != version:
            sections[index], count = re.subn(r'(?m)^version = "[^"\n]+"$', f'version = "{version}"', sections[index], count=1)
            if count != 1:
                raise ValueError("Cannot normalize local workspace lock version")

    return "[[package]]".join(sections)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Trial:
    def __init__(self, work, diagnostics, environment):
        self.work = Path(work)
        self.diagnostics = Path(diagnostics)
        self.environment = dict(environment)
        self.samples = []
        self.commands = []
        self.tests = []

    def sample(self, minimum=MIN_FREE_BYTES):
        free = shutil.disk_usage(self.work).free
        self.samples.append({"elapsed_seconds": round(time.monotonic() - self.started, 2), "free_bytes": free})
        if free < minimum:
            raise RuntimeError(f"Insufficient disk reserve: {free} bytes; required {minimum}")

    def run(self, argv, cwd, *, heavy=False, allow_static_ldd=False) -> str:
        self.sample()
        argv = [str(argument) for argument in argv]
        print("+ " + " ".join(argv), flush=True)
        started = time.monotonic()
        process = subprocess.Popen(argv, cwd=cwd, env=self.environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        errors = []

        def relay_stderr():
            for line in process.stderr:
                errors.append(line)
                print(line, end="", file=sys.stderr, flush=True)

        relay = threading.Thread(target=relay_stderr)
        relay.start()
        chunks = []

        def read_stdout():
            chunks.append(process.stdout.read())

        reader = threading.Thread(target=read_stdout)
        reader.start()

        try:
            while True:
                try:
                    process.wait(timeout=15)
                    break
                except subprocess.TimeoutExpired:
                    self.sample()
            self.sample()
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        finally:
            relay.join()
            reader.join()

        output = "".join(chunks)
        stderr = "".join(errors)
        self.commands.append({"argv": argv, "seconds": round(time.monotonic() - started, 2),
                              "returncode": process.returncode, "heavy": heavy})
        with (self.diagnostics / "stdout.log").open("a") as log:
            log.write("+ " + " ".join(argv) + "\nSTDOUT\n" + output + "\nSTDERR\n" + stderr + "\n")
        if process.returncode and not (allow_static_ldd and process.returncode == 1
                                      and (output + stderr).strip() == "not a dynamic executable"):
            raise RuntimeError(f"Command failed with exit code {process.returncode}: {argv}")

        return output

    def execute(self, root, config):
        self.started = time.monotonic()
        reject_symlinks(self.diagnostics)
        self.diagnostics.mkdir(parents=True, exist_ok=True)
        for name in ("stdout.log", "metrics.json", "cargo-artifacts.jsonl"):
            reject_symlinks(self.diagnostics / name)
        status = "failed"

        try:
            for path in SDK_PATHS:
                reject_symlinks(Path(path))
                self.run(["sudo", "rm", "-rf", "--", path], self.work)
            self.run(["sudo", "apt-get", "update"], self.work)
            self.run(["sudo", "apt-get", "install", "-y", "--no-install-recommends",
                      "pkg-config", "libssl-dev", "libcap-dev", "clang"], self.work)
            self.sample(INITIAL_FREE_BYTES)
            self.build(root, config)
            status = "validated"
        finally:
            metrics = {"status": status, "commands": self.commands, "disk_samples": self.samples,
                       "observed_min_free_bytes": min((sample["free_bytes"] for sample in self.samples), default=None),
                       "sampling_note": "Samples at command boundaries and every 15 seconds; not actual peak usage.",
                       "elapsed_seconds": round(time.monotonic() - self.started, 2)}
            (self.diagnostics / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")

    def build(self, root, config):
        source = self.work / "source"
        self.run(["git", "clone", "--depth=1", "--branch", "rust-v" + config["version"],
                  "https://github.com/openai/codex.git", source], self.work)
        if self.run(["git", "rev-parse", "HEAD"], source).strip() != config["upstream_commit"]:
            raise ValueError("Upstream tag does not match the reviewed commit")
        for name, digest in config["patches"].items():
            if Path(name).name != name:
                raise ValueError("Invalid patch filename")
            patch = root / "patches" / name
            reject_symlinks(patch)
            if sha256(patch) != digest:
                raise ValueError(f"Patch checksum mismatch: {name}")
            self.run(["git", "apply", "--check", patch], source)
            self.run(["git", "apply", patch], source)

        workspace = source / "codex-rs"
        manifest = tomllib.loads((workspace / "Cargo.toml").read_text())
        if manifest["workspace"]["package"]["version"] != config["version"]:
            raise ValueError("Workspace version differs from fixed version")

        self.run(["rustup", "toolchain", "install", config["toolchain"], "--profile", "minimal"], self.work)
        self.environment["RUSTUP_TOOLCHAIN"] = config["toolchain"]
        self.environment["CODEX_REPO_ROOT"] = str(source)
        self.environment["PYTHONPATH"] = str(source / "scripts")
        code = ("import json; from pathlib import Path; from codex_package.targets import TARGET_SPECS; "
                "from codex_package.v8 import resolve_codex_v8_cargo_env; "
                f"print(json.dumps(resolve_codex_v8_cargo_env(TARGET_SPECS[{config['target']!r}], "
                f"cache_root=Path({str(Path(self.environment['CARGO_HOME']).parent / 'v8')!r}))))")
        self.environment.update(json.loads(self.run([sys.executable, "-c", code], self.work)))

        lock = workspace / "Cargo.lock"
        original = lock.read_text()

        try:
            metadata = json.loads(self.run(["cargo", "metadata", "--no-deps", "--format-version", "1"], workspace))
        finally:
            lock.write_text(original)
        members = set(metadata["workspace_members"])
        packages = [package for package in metadata["packages"] if package["id"] in members]
        package_ids = [package["id"] for package in packages if package["name"] == "codex-tui"]
        if len(package_ids) != 1:
            raise ValueError("Cannot identify the exact workspace TUI package")
        lock.write_text(normalize_lock(original, config["version"], {package["name"] for package in packages}))

        if sum(len(group[2]) for group in config["test_groups"]) != 26:
            raise ValueError("The reviewed trial requires 26 focused tests")
        common = ["--locked", "--target", config["target"], "--profile", config["profile"]]
        for package, selector, required in config["test_groups"]:
            command = ["cargo", "test", *common, "-p", package,
                       "--lib" if package == "codex-tui" else "--bin=codex"]
            listing = self.run([*command, selector, "--", "--list"], workspace, heavy=True)
            names = select_test_names(listing, required)
            output = self.run([*command, "--", "--exact", "--test-threads=1", *names], workspace, heavy=True)
            validate_test_run(output, names)
            self.tests.append({"package": package, "selector": selector, "tests": names, "passed": len(names)})

        command = ["cargo", "build", *common, "--message-format=json"]
        for binary in BINARIES:
            command.extend(["--bin", binary])
        output = self.run(command, workspace, heavy=True)
        validate_profile(output, package_ids[0])
        (self.diagnostics / "cargo-artifacts.jsonl").write_text(output)

        artifacts = Path(self.environment["CARGO_TARGET_DIR"]) / config["target"] / config["profile"]
        package_dir = self.work / "package"
        self.run([sys.executable, source / "scripts/build_codex_package.py", "--target", config["target"],
                  "--cargo-profile", config["profile"], "--package-version", config["version"],
                  "--package-dir", package_dir, "--entrypoint-bin", artifacts / "codex",
                  "--code-mode-host-bin", artifacts / "codex-code-mode-host", "--exec-server-bin",
                  artifacts / "codex-exec-server", "--bwrap-bin", artifacts / "bwrap", "--strip", "all"], self.work)
        for name in ("LICENSE", "NOTICE"):
            shutil.copy2(source / name, package_dir / name)

        probe_environment = self.environment
        self.environment = isolated_environment(self.work / "probe")

        try:
            cwd = self.work / "probe/cwd"
            if self.run([package_dir / "bin/codex", "--version"], cwd).strip() != "codex-cli " + config["version"]:
                raise ValueError("Packaged CLI version differs from the fixed version")
            binary_hashes = {}
            for directory in ("bin", "codex-resources", "codex-path"):
                for binary in sorted((package_dir / directory).iterdir()):
                    if binary.is_file():
                        output = self.run(["ldd", binary], cwd, allow_static_ldd=True)
                        if "not found" in output:
                            raise ValueError(f"Unresolved runtime library: {binary.name}")
                        binary_hashes[str(binary.relative_to(package_dir))] = sha256(binary)
        finally:
            self.environment = probe_environment

        payload = self.work.parent / "payload"
        reject_symlinks(payload)
        payload.mkdir()
        archive = payload / "codex-trial.tar.gz"
        code = ("from pathlib import Path; from codex_package.archive import write_archive; "
                f"write_archive(Path({str(package_dir)!r}), Path({str(archive)!r}), force=False)")
        self.run([sys.executable, "-c", code], self.work)

        evidence = {"status": "validated", "config": config, "tests": self.tests, "binaries": binary_hashes,
                    "archive_sha256": sha256(archive), "commands": self.commands, "disk_samples": self.samples,
                    "sampling_note": "Observed samples only; not actual peak disk usage."}
        build_manifest = payload / "build-manifest.json"
        build_manifest.write_text(json.dumps(evidence, indent=2) + "\n")
        (payload / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in (archive, build_manifest)))


def main() -> None:
    runner_temp = assert_hosted_runner(os.environ)
    dependencies = runner_temp / "codex-trial-dependencies"
    reject_symlinks(dependencies)
    if sys.argv[1:] == ["--cache-check"]:
        size = 0
        if dependencies.exists():
            for path in dependencies.rglob("*"):
                reject_symlinks(path)
                if path.is_file():
                    size += path.stat().st_size
        output = Path(os.environ["GITHUB_OUTPUT"])
        reject_symlinks(output)
        with output.open("a") as stream:
            stream.write(f"save={'true' if size <= CACHE_LIMIT_BYTES else 'false'}\n")

        return

    if sys.argv[1:]:
        raise ValueError("Unsupported trial arguments")
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "build-config.json").read_text())
    work = Path(tempfile.mkdtemp(prefix="codex-trial-", dir=runner_temp))
    environment = isolated_environment(work)
    environment.update({"CARGO_HOME": str(dependencies / "cargo"), "RUSTUP_HOME": str(work / "rustup"),
                        "CARGO_TARGET_DIR": str(work / "target"), "CARGO_BUILD_JOBS": "2",
                        "CARGO_INCREMENTAL": "0", "CARGO_PROFILE_DEV_SMALL_DEBUG_ASSERTIONS": "false",
                        "CARGO_PROFILE_DEV_SMALL_LTO": "false", "RUST_MIN_STACK": "8388608"})
    Trial(work, runner_temp / "codex-trial-diagnostics", environment).execute(root, config)


if __name__ == "__main__":
    main()
