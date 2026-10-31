import plistlib


def test_launch_agent_passes_mac_opt_in_without_embedding_secret(tmp_path) -> None:
    from personal_data_platform.sources.mac_screen_time.launchd import (
        LaunchAgentSettings,
        build_launch_agent,
    )

    configured = {"MOTHERDUCK_DATABASE": "production"}
    configured["PDP_MAC_SCREEN_TIME_DEVICE_KEY"] = "c" * 64
    configured["PDP_MAC_SCREEN_TIME_APP_USAGE_LOCAL_DIR"] = str(tmp_path / "AppUsage/local")
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='test'\n")
    settings = LaunchAgentSettings.from_env(project_root=project, environ=configured)

    environment = plistlib.loads(build_launch_agent(settings))["EnvironmentVariables"]
    assert environment["PDP_MAC_SCREEN_TIME_DEVICE_KEY"] == "c" * 64
    assert (
        environment["PDP_MAC_SCREEN_TIME_APP_USAGE_LOCAL_DIR"]
        == configured["PDP_MAC_SCREEN_TIME_APP_USAGE_LOCAL_DIR"]
    )
    assert "PDP_MAC_SCREEN_TIME_PSEUDONYM_KEY_HEX" not in environment
