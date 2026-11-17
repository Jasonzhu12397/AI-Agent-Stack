from apps.node_agent.runtime import main as run_node_agent


def main() -> None:
    run_node_agent(expected_node_type="eph_node")


if __name__ == "__main__":
    main()
