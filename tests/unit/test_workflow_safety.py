from pathlib import Path

import yaml


WORKFLOW_PATH = Path(__file__).parents[2] / ".github" / "workflows" / "sync.yml"


def test_manual_workflow_is_non_mutating_timestamp_capability_check_only():
    workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch"}

    run_commands = [
        step["run"]
        for step in workflow["jobs"]["check"]["steps"]
        if "run" in step
    ]
    sync_command = next(
        command for command in run_commands
        if "python -m spotify_to_tidal" in command
    )
    assert "--check-spotify-timestamp-support" in sync_command
    assert "--sync-favorites" not in sync_command
    assert "--sync-direction tidal_to_spotify" not in sync_command
    assert "--sync-direction bidirectional" not in sync_command
    assert "--approved-favorites-plan" not in sync_command
    assert "--uri" not in sync_command

    environment = workflow["jobs"]["check"]["env"]
    assert not any(name.startswith("TIDAL_") for name in environment)
