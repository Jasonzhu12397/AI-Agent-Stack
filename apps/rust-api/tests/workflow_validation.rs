use ai_agent_stack_api_edge::workflow_validation::{execution_layers, validate};
use serde_json::json;

fn graph(start: &str, end: &str, extra: serde_json::Value) -> serde_json::Value {
    let mut nodes = vec![json!({"id": start, "type": "start", "config": {}})];
    if let Some(extra) = extra.as_array() {
        nodes.extend(extra.clone());
    }
    nodes.push(json!({"id": end, "type": "end", "config": {}}));
    json!({
        "nodes": nodes,
        "edges": [{"source": start, "target": end}]
    })
}

#[test]
fn accepts_a_minimal_graph_and_computes_layers() {
    let value = graph("start", "end", json!([]));
    let graph = validate(&value).unwrap();
    let layers = execution_layers(&graph);
    assert_eq!(
        layers,
        vec![vec!["start".to_string()], vec!["end".to_string()]]
    );
}

#[test]
fn rejects_duplicate_ids_and_cycles() {
    let duplicate = json!({
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "start", "type": "end", "config": {}}
        ],
        "edges": []
    });
    assert!(validate(&duplicate).unwrap_err().contains("Duplicate"));

    let cycle = json!({
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "a", "type": "approval", "config": {}},
            {"id": "end", "type": "end", "config": {}}
        ],
        "edges": [
            {"source": "start", "target": "a"},
            {"source": "a", "target": "a"}
        ]
    });
    assert!(validate(&cycle).unwrap_err().contains("Self edges"));
}

#[test]
fn rejects_credentials_and_bad_ui_positions() {
    let credential = graph(
        "start",
        "end",
        json!([{"id": "llm", "type": "llm", "config": {
            "model_config_id": "11111111-1111-1111-1111-111111111111",
            "prompt_template": "hi",
            "api_key": "nope"
        }}]),
    );
    assert!(validate(&credential).unwrap_err().contains("credential"));

    let positions = json!({
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "end", "type": "end", "config": {}}
        ],
        "edges": [{"source": "start", "target": "end"}],
        "ui": {"node_positions": {"start": {"x": -1, "y": 0}}}
    });
    assert!(validate(&positions).unwrap_err().contains("out of range"));
}

#[test]
fn requires_condition_branches_and_end_nodes() {
    let condition = json!({
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "choose", "type": "condition", "config": {"equals": true}},
            {"id": "yes", "type": "end", "config": {}},
            {"id": "no", "type": "end", "config": {}}
        ],
        "edges": [
            {"source": "start", "target": "choose"},
            {"source": "choose", "target": "yes", "when": true}
        ]
    });
    assert!(validate(&condition).is_err());
}
