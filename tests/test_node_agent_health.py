from apps.node_agent import agent


def test_agent_main_refreshes_readiness_before_polling(monkeypatch):
    readiness_updates = []

    class Runtime:
        def run_once(self):
            return True

    monkeypatch.setattr(
        agent,
        "ControlPlaneClient",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        agent,
        "create_agent",
        lambda client, node_type: Runtime(),
    )
    monkeypatch.setattr(
        agent,
        "touch_health_files",
        lambda readiness=False: readiness_updates.append(readiness),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["agent", "--once"],
    )
    monkeypatch.setenv("AGENT_CONTROL_URL", "https://control.example.test")
    monkeypatch.setenv("AGENT_NODE_ID", "eph-test-node")
    monkeypatch.setenv("AGENT_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_NODE_TYPE", "eph_node")
    monkeypatch.setenv("APP_ENV", "production")

    agent.main()

    assert readiness_updates
    assert readiness_updates[0] is True
