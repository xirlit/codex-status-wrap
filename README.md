# Codex status wrapping — hosted build trial

Experimental patches for [OpenAI Codex](https://github.com/openai/codex). This repository builds a patched Linux executable on GitHub Actions, keeping compilation off the user's machine. This is not an official OpenAI distribution.

The trial uses reviewed `rust-v0.167.2`, commit `ce21072c90d32c40af7a55b60b2fb35c84a43a6a`. Exact toolchain, patch hashes and the 26 required tests are recorded in `build-config.json`.

## Current status

The public repository `xirlit/codex-status-wrap` is approved, with the GitHub connection pending. This tooling has not been published or run on GitHub Actions, and no hosted artifact is available yet. The working daily runtime remains 0.167.1.

## Run

After publication, open **Actions**, choose **Build patched Codex trial**, then **Run workflow** on the default branch. This manual dispatch is the only trigger. After success, download `codex-validated-trial`; `codex-trial-diagnostics` contains command durations and observed disk samples, including failures. Artifacts expire after three days.

The workflow runs the lightweight automation tests before restoring dependencies or starting the heavy build. It uses a standard GitHub-hosted Ubuntu 24.04 x64 runner, with a 90-minute limit. It removes only a fixed list of unused preinstalled SDKs on that disposable runner and installs native prerequisites. Initial free disk must be at least 28 GiB; subsequent commands retain a 4 GiB reserve. Public dependency downloads are cached only if their total is at most 8 GiB. Compiler targets are not cached in this first measurement. No particular duration is promised before a measured run.

The executable and three helpers are built together. Actual Cargo artifact metadata verifies production debug assertions are disabled, avoiding a separate flag-check compilation. All 26 exact regression cases must pass, including wrapping, Unicode, oversize fields, resize/footer height, hints, and custom-update target execution. Packaging reuses the upstream builder with prebuilt binaries and preserves upstream notices.

## Scope

Only public patches, build tooling and license notices belong here. Codex settings, conversations, credentials and user projects are not inputs. The workflow needs read-only repository permissions; it does not publish releases or use personal secrets.

The package is Linux x86_64 GNU, built on Ubuntu 24.04. Check its compatibility before replacing any working package. The successful payload includes an archive, SHA-256 checksums and build evidence. Ordinary files on the Windows D: drive can hold the runtime; a separate Linux build VHD is not required for daily launches.

Automatic downloads/updates, upstream polling, native Windows binaries, PATH changes and local activation are outside this first trial. The included native-update patch prepares a custom action, but a download-only updater is not yet complete. Future patch conflicts can delay an updated package; keep the current working version until a new one passes validation.

## Lightweight checks

```sh
python3 -B -m unittest discover -s tests -v
```

The build entry point rejects execution outside a GitHub-hosted Linux runner. Do not bypass that guard to compile locally.

## License

The patches modify Apache-2.0-licensed OpenAI Codex. See `LICENSE` and `NOTICE`; new build tooling uses the same license. This repository is independent of OpenAI.
