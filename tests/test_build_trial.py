import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import unittest.mock
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_trial

EXPECTED_TESTS_BY_PACKAGE = {
    "codex-tui": {
        "custom_target_preserves_exact_stable_version",
        "custom_target_rejects_malformed_versions",
        "custom_marker_requires_an_absolute_root",
        "custom_prompt_binds_displayed_target_and_preserves_skip_choices",
        "custom_prompt_rejects_invalid_target_before_display",
        "custom_notice_binds_the_displayed_version",
        "custom_notice_rejects_malformed_targets",
        "custom_notice_preserves_standard_update_commands",
        "token_usage_update_refreshes_status_line_with_runtime_context_window",
        "maps_install_context_to_update_action",
        "status_fields_pack_whole_fields_at_exact_boundaries_and_reflow",
        "status_fields_preserve_embedded_separators_and_unicode_display_widths",
        "oversized_fields_are_bounded_without_losing_subsequent_fields",
        "wrapped_status_fields_preserve_styles_and_link_offsets_without_linking_ellipsis",
        "both_configured_renderers_preserve_field_boundaries_and_links",
        "clipped_links_keep_their_cells_after_a_halfwidth_katakana_grapheme",
        "arabic_prefix_keeps_link_on_the_painted_cells_at_exact_fit_and_overflow",
        "configured_extension_status_fields_wrap_with_complete_links",
        "configured_fields_resize_without_overlapping_composer_input",
        "wrapped_status_keeps_warning_and_transient_hint_priority",
        "effort_transition_preserves_following_wrapped_status_rows",
        "wrapped_status_reserves_the_compact_right_indicator_on_its_final_row",
        "configured_renderers_retain_their_existing_active_agent_suffix_policy",
        "empty_configured_renderers_preserve_active_agent_suffix_behavior",
    },
    "codex-cli": {
        "custom_updater_rejects_missing_relative_and_unavailable_roots",
        "custom_update_uses_fixed_updater_exact_argv_and_propagates_failure",
    },
}

EXPECTED_TEST_COUNT = 26
TUI_PACKAGE_ID = "path+file:///runner/codex-rs/tui#codex-tui@0.167.2"


class BuildTrialTests(unittest.TestCase):
    def testIndependentExpectedTestInventoryHasAll26CasesAndBothOwners(self):
        self.assertEqual(EXPECTED_TEST_COUNT, sum(map(len, EXPECTED_TESTS_BY_PACKAGE.values())))
        self.assertEqual({"codex-tui", "codex-cli"}, set(EXPECTED_TESTS_BY_PACKAGE))

    def testConfigTestGroupsContainIndependentExpectedCasesUnderCorrectOwners(self):
        config_path = Path(__file__).resolve().parents[1] / "build-config.json"
        config = json.loads(config_path.read_text())
        selected_by_package = {}
        for package, _selector, names in config["test_groups"]:
            selected_by_package.setdefault(package, set()).update(names)

        for package, expected_names in EXPECTED_TESTS_BY_PACKAGE.items():
            self.assertTrue(
                expected_names.issubset(selected_by_package.get(package, set())),
                f"The configured test plan is missing required cases for {package}",
            )
        configured_names = [
            name
            for _package, _selector, names in config["test_groups"]
            for name in names
        ]
        self.assertEqual(EXPECTED_TEST_COUNT, len(configured_names))
        self.assertEqual(EXPECTED_TEST_COUNT, len(set(configured_names)))

    def testConfigPinsReviewedVersionCommitToolchainTargetProfileAndPatchHashes(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "build-config.json").read_text())
        expected_patch_hashes = {
            "status-wrap.patch": "ae2630f60d887552d73c0a975652365faa05fab43098dad2567a65a1f17a11e5",
            "native-update.patch": "b4a35b96e1dfac1d21e2b0a7e5e3083ce2a122e24ccaa700501200fc38f556d3",
        }

        self.assertEqual("0.167.2", config["version"])
        self.assertEqual("ce21072c90d32c40af7a55b60b2fb35c84a43a6a", config["upstream_commit"])
        self.assertEqual("1.95.0", config["toolchain"])
        self.assertEqual("x86_64-unknown-linux-gnu", config["target"])
        self.assertEqual("dev-small", config["profile"])
        self.assertEqual(expected_patch_hashes, config["patches"])
        for name, expected_digest in expected_patch_hashes.items():
            actual_digest = hashlib.sha256((root / "patches" / name).read_bytes()).hexdigest()
            self.assertEqual(expected_digest, actual_digest, name)

    def testSelectTestNamesSelectsAllRequiredCasesExactlyOnce(self):
        listing = "\n".join(
            f"tests::{name}: test"
            for names in EXPECTED_TESTS_BY_PACKAGE.values()
            for name in sorted(names)
        )

        selected = build_trial.select_test_names(
            listing,
            [name for names in EXPECTED_TESTS_BY_PACKAGE.values() for name in sorted(names)],
        )

        self.assertEqual(EXPECTED_TEST_COUNT, len(selected))
        self.assertEqual(EXPECTED_TEST_COUNT, len(set(selected)))

    def testSelectTestNamesRejectsMissingAndAmbiguousMatches(self):
        with self.assertRaises(ValueError):
            build_trial.select_test_names("tests::one: test", ["missing"])
        with self.assertRaises(ValueError):
            build_trial.select_test_names(
                "first::wanted: test\nsecond::wanted: test", ["wanted"]
            )

    def testValidateTestRunRequiresEveryCaseToPassAndRejectsZeroCases(self):
        passing = "\n".join(
            [*(f"test {name} ... ok" for name in sorted(EXPECTED_TESTS_BY_PACKAGE["codex-cli"])),
             "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"]
        )
        build_trial.validate_test_run(passing, sorted(EXPECTED_TESTS_BY_PACKAGE["codex-cli"]))

        with self.assertRaises(ValueError):
            build_trial.validate_test_run(
                "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out", []
            )
        with self.assertRaises(ValueError):
            build_trial.validate_test_run(
                "test test_a ... FAILED\ntest result: FAILED. 0 passed; 1 failed",
                ["test_a"],
            )
        with self.assertRaises(ValueError):
            build_trial.validate_test_run(
                "test test_a ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored",
                ["test_a", "test_b"],
            )

    def testValidateProfileRequiresExactPackageAndNonTestCodexTuiLibraryArtifact(self):
        artifact = {
            "reason": "compiler-artifact",
            "package_id": TUI_PACKAGE_ID,
            "target": {"name": "codex_tui", "kind": ["lib"]},
            "profile": {"test": False, "debug_assertions": False},
            "executable": "/runner/target/codex_tui",
        }
        build_trial.validate_profile(json.dumps(artifact), TUI_PACKAGE_ID)

        invalid_artifacts = [
            None,
            {**artifact, "package_id": "wrong-package"},
            {**artifact, "target": {"name": "codex_tui", "kind": ["bin"]}},
            {**artifact, "target": {"name": "other", "kind": ["lib"]}},
            {**artifact, "profile": {"test": True, "debug_assertions": False}},
            {**artifact, "profile": {"test": False, "debug_assertions": True}},
        ]
        for invalid in invalid_artifacts:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    build_trial.validate_profile(json.dumps(invalid), TUI_PACKAGE_ID)

    def testValidateProfileRejectsMissingMalformedAndAmbiguousEvidence(self):
        artifact = {
            "reason": "compiler-artifact",
            "package_id": TUI_PACKAGE_ID,
            "target": {"name": "codex_tui", "kind": ["lib"]},
            "profile": {"test": False, "debug_assertions": False},
            "executable": None,
        }
        for output in ("", "not json", json.dumps({"reason": "build-finished"})):
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    build_trial.validate_profile(output, TUI_PACKAGE_ID)
        with self.assertRaises(ValueError):
            build_trial.validate_profile(
                "\n".join((json.dumps(artifact), json.dumps(artifact))), TUI_PACKAGE_ID
            )

    def testNormalizeLockChangesOnlyNamedSourceLessWorkspacePackages(self):
        lock = (
            '[[package]]\nname = "codex-tui"\nversion = "0.0.0"\n\n'
            '[[package]]\nname = "codex-cli"\nversion = "0.0.0"\n\n'
            '[[package]]\nname = "external"\nversion = "0.0.0"\n'
            'source = "registry+https://example.invalid/index"\n'
        )

        normalized = build_trial.normalize_lock(
            lock, "0.167.2", {"codex-tui", "codex-cli"}
        )

        self.assertIn('name = "codex-tui"\nversion = "0.167.2"', normalized)
        self.assertIn('name = "codex-cli"\nversion = "0.167.2"', normalized)
        self.assertIn(
            'name = "external"\nversion = "0.0.0"\n'
            'source = "registry+https://example.invalid/index"',
            normalized,
        )

    def testBuildSetsControlledRepositoryRootBeforeV8Resolver(self):
        class StopAtV8Resolver(Exception):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "work"
            work.mkdir()
            source = work / "source"
            workspace = source / "codex-rs"
            config = {
                "version": "0.167.2",
                "upstream_commit": "reviewed-commit",
                "toolchain": "1.95.0",
                "target": "x86_64-unknown-linux-gnu",
                "patches": {},
            }
            trial = build_trial.Trial(
                work,
                work / "diagnostics",
                {"CARGO_HOME": str(work / "cargo-home"), "CODEX_REPO_ROOT": "/untrusted/root"},
            )

            def fake_run(argv, _cwd, **_kwargs):
                if argv[0] == "git" and argv[1] == "clone":
                    workspace.mkdir(parents=True)
                    (workspace / "Cargo.toml").write_text(
                        '[workspace.package]\nversion = "0.167.2"\n'
                    )
                    return ""
                if argv[:3] == ["git", "rev-parse", "HEAD"]:
                    return config["upstream_commit"]
                if argv[0] == "rustup":
                    return ""
                if argv[0] == sys.executable:
                    self.assertEqual(str(source), trial.environment["CODEX_REPO_ROOT"])
                    self.assertEqual(str(source / "scripts"), trial.environment["PYTHONPATH"])
                    raise StopAtV8Resolver
                self.fail(f"Unexpected build command before V8 resolution: {argv}")

            with patch.object(trial, "run", side_effect=fake_run):
                with self.assertRaises(StopAtV8Resolver):
                    trial.build(Path(temporary), config)

    def testIsolatedEnvironmentUsesFreshHomeAndCodexHome(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            with unittest.mock.patch.dict(
                os.environ,
                {"HOME": "/untrusted/home", "CODEX_HOME": "/untrusted/codex"},
            ):
                environment = build_trial.isolated_environment(work)

            self.assertNotEqual("/untrusted/home", environment["HOME"])
            self.assertNotEqual("/untrusted/codex", environment["CODEX_HOME"])
            for name in ("HOME", "CODEX_HOME"):
                path = Path(environment[name])
                self.assertTrue(path.is_dir())
                self.assertTrue(path.is_relative_to(work))
            self.assertTrue((work / "cwd").is_dir())

    def testIsolatedEnvironmentRejectsSymlinkedWorkRoots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            link = root / "work"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaises((ValueError, OSError)):
                build_trial.isolated_environment(link)

    def testHostedRunnerRequiresHostedLinuxMarkersAndExistingRunnerTemp(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner_temp = Path(temporary)
            environment = {
                "GITHUB_ACTIONS": "true",
                "RUNNER_ENVIRONMENT": "github-hosted",
                "RUNNER_OS": "Linux",
                "RUNNER_TEMP": str(runner_temp),
            }
            self.assertEqual(runner_temp, build_trial.assert_hosted_runner(environment))
            for key, value in (
                ("GITHUB_ACTIONS", "false"),
                ("RUNNER_ENVIRONMENT", "self-hosted"),
                ("RUNNER_OS", "Windows"),
                ("RUNNER_TEMP", str(runner_temp / "missing")),
            ):
                invalid = environment | {key: value}
                with self.subTest(key=key):
                    with self.assertRaises((ValueError, RuntimeError)):
                        build_trial.assert_hosted_runner(invalid)

    def testDiskGuardsUseDistinctInitialAndOngoingThresholdsWithInclusiveBoundary(self):
        gib = 1024 ** 3
        self.assertEqual(28 * gib, build_trial.INITIAL_FREE_BYTES)
        self.assertEqual(4 * gib, build_trial.MIN_FREE_BYTES)
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            trial = build_trial.Trial(work, work / "diagnostics", {})
            trial.started = 0
            with patch.object(
                build_trial.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * gib)
            ):
                trial.sample()
            with patch.object(
                build_trial.shutil, "disk_usage", return_value=SimpleNamespace(free=4 * gib)
            ):
                trial.sample()
            with patch.object(
                build_trial.shutil, "disk_usage", return_value=SimpleNamespace(free=28 * gib)
            ):
                trial.sample(build_trial.INITIAL_FREE_BYTES)
            with patch.object(
                build_trial.shutil, "disk_usage", return_value=SimpleNamespace(free=4 * gib - 1)
            ):
                with self.assertRaises(RuntimeError):
                    trial.sample()
            with patch.object(
                build_trial.shutil, "disk_usage", return_value=SimpleNamespace(free=28 * gib - 1)
            ):
                with self.assertRaises(RuntimeError):
                    trial.sample(build_trial.INITIAL_FREE_BYTES)

    def testExecuteChecksInitialDiskAfterPrerequisitesAndBeforeBuild(self):
        initial_minimum = build_trial.INITIAL_FREE_BYTES
        config = {"version": "0.167.2"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trial = build_trial.Trial(root / "work", root / "diagnostics", {})
            trial.work.mkdir()
            events = []

            def record_run(argv, _cwd, **_kwargs):
                events.append(("run", argv[0]))
                return ""

            def allow_initial_space(minimum):
                events.append(("sample", minimum))

            with patch.object(build_trial, "reject_symlinks"), patch.object(
                trial, "run", side_effect=record_run
            ), patch.object(trial, "sample", side_effect=allow_initial_space), patch.object(
                trial, "build", side_effect=lambda *_args: events.append(("build", None))
            ):
                trial.execute(root, config)

            initial_check_index = events.index(("sample", initial_minimum))
            build_index = events.index(("build", None))
            self.assertLess(initial_check_index, build_index)
            self.assertEqual(5, sum(event == ("run", "sudo") for event in events))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trial = build_trial.Trial(root / "work", root / "diagnostics", {})
            trial.work.mkdir()
            events = []

            def reject_initial_space(minimum):
                events.append(("sample", minimum))
                if minimum == initial_minimum:
                    raise RuntimeError("initial disk space is insufficient")

            with patch.object(build_trial, "reject_symlinks"), patch.object(
                trial, "run", side_effect=lambda argv, _cwd, **_kwargs: events.append(("run", argv[0]))
            ), patch.object(trial, "sample", side_effect=reject_initial_space), patch.object(
                trial, "build", side_effect=lambda *_args: events.append(("build", None))
            ) as build:
                with self.assertRaisesRegex(RuntimeError, "initial disk space"):
                    trial.execute(root, config)

            build.assert_not_called()
            self.assertNotIn(("build", None), events)
            self.assertEqual("failed", json.loads(
                (root / "diagnostics" / "metrics.json").read_text()
            )["status"])

    def testOngoingDiskGuardStopsCommandBeforeSpawnBelowFourGiB(self):
        gib = 1024 ** 3
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            diagnostics = work / "diagnostics"
            diagnostics.mkdir()
            trial = build_trial.Trial(work, diagnostics, {})
            trial.started = 0
            with patch.object(
                build_trial.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=4 * gib - 1),
            ), patch.object(build_trial.subprocess, "Popen") as spawn:
                with self.assertRaises(RuntimeError):
                    trial.run(["cargo", "build"], work)

            spawn.assert_not_called()

    def testRunReturnsMachineReadableStdoutWhileStderrRemainsLive(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            diagnostics = work / "diagnostics"
            diagnostics.mkdir()
            trial = build_trial.Trial(work, diagnostics, {})
            artifact_output = json.dumps({
                "reason": "compiler-artifact",
                "package_id": TUI_PACKAGE_ID,
                "target": {"name": "codex_tui", "kind": ["lib"]},
                "profile": {"test": False, "debug_assertions": False},
            })
            process = unittest.mock.Mock()
            process.stdout = io.StringIO(artifact_output)
            process.stderr = io.StringIO("cargo progress\n")
            process.returncode = 0
            with patch.object(trial, "sample"), patch.object(
                build_trial.subprocess, "Popen", return_value=process
            ) as popen:
                output = trial.run(["cargo", "build"], work, heavy=True)

            self.assertEqual(artifact_output, output)
            build_trial.validate_profile(output, TUI_PACKAGE_ID)
            self.assertEqual(build_trial.subprocess.PIPE, popen.call_args.kwargs["stderr"])
            log = (diagnostics / "stdout.log").read_text()
            self.assertIn(f"STDOUT\n{artifact_output}\nSTDERR", log)
            self.assertIn("STDERR\ncargo progress", log)

    def testProductionProfileFailurePreventsArchiveAndValidatedMetrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "work"
            work.mkdir()
            diagnostics = Path(temporary) / "diagnostics"
            trial = build_trial.Trial(work, diagnostics, {})
            config = {"version": "0.167.2"}
            with patch.object(trial, "run"), patch.object(trial, "sample"), patch.object(
                trial,
                "build",
                side_effect=lambda _root, _config: build_trial.validate_profile(
                    "{}", TUI_PACKAGE_ID
                ),
            ):
                with self.assertRaises(ValueError):
                    trial.execute(Path(temporary), config)

            self.assertFalse((Path(temporary) / "payload").exists())
            self.assertEqual(
                "failed", json.loads((diagnostics / "metrics.json").read_text())["status"]
            )

    def testWorkflowIsManualReadOnlyPinnedAndKeepsArtifactsForThreeDays(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/build.yml").read_text()
        lines = workflow.splitlines()
        trigger_start = lines.index("on:")
        trigger_lines = []
        for line in lines[trigger_start + 1:]:
            if line and not line.startswith(" "):
                break
            if line.startswith("  ") and not line.startswith("    "):
                trigger_lines.append(line.strip())

        self.assertEqual(["workflow_dispatch:"], trigger_lines)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("retention-days: 3", workflow)
        self.assertNotIn("secrets.", workflow)
        for action_sha in (
            "de0fac2e4500dabe0009e67214ff5f5447ce83dd",
            "55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
            "bbbca2ddaa5d8feaa63e36b76fdaad77386f024f",
        ):
            self.assertIn(action_sha, workflow)
        self.assertIn("path: ${{ runner.temp }}/payload/", workflow)
        self.assertLess(
            workflow.index("run: python3 scripts/build_trial.py"),
            workflow.index("name: Upload validated trial"),
        )

    def testMainRejectsWrongHostBeforeFilesystemOrSubprocessEffects(self):
        # The CLI contract is intentionally fail-closed outside a verified hosted runner.
        with unittest.mock.patch.dict(os.environ, {}, clear=True), patch.object(
            build_trial.subprocess, "Popen"
        ) as spawn, patch.object(build_trial.tempfile, "mkdtemp") as make_temp:
            with self.assertRaises((ValueError, RuntimeError, SystemExit)):
                build_trial.main()

        spawn.assert_not_called()
        make_temp.assert_not_called()
