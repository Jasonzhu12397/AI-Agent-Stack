from apps.node_agent.runtime import main as run_node_agent


def main() -> None:
    run_node_agent(expected_node_type="target_cluster")


if __name__ == "__main__":
    main()
