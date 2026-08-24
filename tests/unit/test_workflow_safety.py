from pathlib import Path

import yaml


WORKFLOW_PATH = Path(__file__).parents[2] / ".github" / "workflows" / "sync.yml"


def test_first_production_sync_is_manual_tidal_to_spotify_favorites_only():
    workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch"}

    run_commands = [
        step["run"]
        for step in workflow["jobs"]["sync"]["steps"]
        if "run" in step
    ]
    sync_command = next(
        command for command in run_commands
        if "python -m spotify_to_tidal" in command
    )
    assert "--sync-favorites" in sync_command
    assert "--sync-direction tidal_to_spotify" in sync_command
    assert "--sync-direction bidirectional" not in sync_command
    assert "--approved-favorites-plan" not in sync_command
    assert "--uri" not in sync_command
