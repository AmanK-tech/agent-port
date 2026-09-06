# Release checklist

Agent Port CLI and plugin releases use one lockstep semantic version. Tag publication is automated,
but the protected `pypi` GitHub environment must approve the publish job.

## Preflight

1. Confirm `pyproject.toml`, `agent_port.__version__`, and both plugin manifests contain the intended version.
2. Synchronize the locked development environment.
3. Run linting and formatting checks for `src` and `tests`, then type checking, tests, and
   coverage. Document-generation utilities under `tools` are not part of release artifacts.
4. Run `python tools/validate_plugins.py` and validate the legacy skill.
5. When Claude Code is installed, run `claude plugin validate plugins/agent-port --strict`.
6. Build the source distribution, wheel, plugin ZIP, and checksums.
7. On two physical computers, test plugin-guided transfer through mDNS and the explicit
   `--host`/`--port` fallback with both supported harnesses. Confirm receipt does not mutate the
   destination harness, and exercise protected code-file consumption, persistent workspace
   retention, interruption, and retry before expiry.

## Artifact checks

1. Confirm the source distribution contains the portable skill, documentation, tests, license,
   README, lockfile, and Python source.
2. Confirm the wheel contains only the intended `agent_port` package and distribution metadata.
3. Install the wheel into a clean temporary environment.
4. Run `agent-port --version` and `agent-port --help` from that environment.
5. Confirm help advertises transfer prepare/receive, inspect, backup, restore plan-info,
   handoff/verify, doctor, and skills.
6. Add both local marketplaces to isolated harness homes and confirm all six plugin skills appear.

## Publish boundary

Review built artifacts before creating a `vMAJOR.MINOR.PATCH` tag. The tag must match every version
contract. Never include local harness state, archives, restore plans, run directories, reports,
generated office documents, caches, or credentials in a release.

PyPI Trusted Publishing and the protected `pypi` environment must be configured before the first
tag. The workflow publishes the wheel and source distribution, creates the plugin ZIP and SHA-256
file, and attaches all artifacts to the GitHub release.

After publication, install the exact release from PyPI into a clean environment, run `pip check`,
verify `agent-port --version` and top-level help, then inspect a synthetic `.agentpack`. Verify the
GitHub release contains the plugin ZIP and matching checksums before announcing the release.
