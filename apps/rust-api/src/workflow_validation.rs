use std::collections::{HashMap, HashSet, VecDeque};

use serde_json::{json, Map, Value};
use sqlx::Row;

const FORBIDDEN_CREDENTIAL_KEYS: [&str; 12] = [
    "api_key",
    "secret",
    "password",
    "access_token",
    "client_secret",
    "authorization",
    "api-key",
    "x-api-key",
    "clientsecret",
    "refresh_token",
    "private_key",
    "bearer_token",
];

const NODE_TYPES: [&str; 9] = [
    "start",
    "llm",
    "condition",
    "knowledge_search",
    "tool",
    "agent",
    "approval",
    "loop",
    "end",
];

#[derive(Clone, Debug)]
pub struct Node {
    pub id: String,
    pub node_type: String,
    pub config: Map<String, Value>,
}

#[derive(Clone, Debug)]
pub struct Edge {
    pub source: String,
    pub target: String,
    pub when: Option<bool>,
}

#[derive(Clone, Debug)]
pub struct Graph {
    pub nodes: HashMap<String, Node>,
    pub edges: Vec<Edge>,
    pub incoming: HashMap<String, Vec<Edge>>,
    pub outgoing: HashMap<String, Vec<Edge>>,
    pub order: Vec<String>,
    pub start_id: String,
}

pub fn validate(definition: &Value) -> Result<Graph, String> {
    let Some(object) = definition.as_object() else {
        return Err("Workflow definition must be a JSON object.".into());
    };
    let nodes_value = object.get("nodes");
    let edges_value = object.get("edges");
    let nodes = nodes_value
        .and_then(Value::as_array)
        .ok_or_else(|| "Workflow must contain between 2 and 50 nodes.".to_string())?;
    if !(2..=50).contains(&nodes.len()) {
        return Err("Workflow must contain between 2 and 50 nodes.".into());
    }
    let edges = edges_value
        .and_then(Value::as_array)
        .ok_or_else(|| "Workflow edges must be an array of at most 200 items.".to_string())?;
    if edges.len() > 200 {
        return Err("Workflow edges must be an array of at most 200 items.".into());
    }
    let mut node_map: HashMap<String, Node> = HashMap::new();
    for node in nodes {
        let Some(node_object) = node.as_object() else {
            return Err("Every workflow node must be an object.".into());
        };
        let node_id = node_object
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| valid_id(id))
            .ok_or_else(|| "Every node requires a valid stable id.".to_string())?;
        if node_map.contains_key(node_id) {
            return Err(format!("Duplicate workflow node id: {node_id}"));
        }
        let node_type = node_object
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !NODE_TYPES.contains(&node_type) {
            return Err(format!("Unsupported workflow node type: {node_type}"));
        }
        let config = match node_object.get("config") {
            Some(Value::Object(map)) => map.clone(),
            Some(_) => return Err(format!("Node {node_id} config must be an object.")),
            None => Map::new(),
        };
        validate_config(node_id, node_type, &config)?;
        node_map.insert(
            node_id.to_string(),
            Node {
                id: node_id.to_string(),
                node_type: node_type.to_string(),
                config,
            },
        );
    }
    validate_ui(object, &node_map)?;
    let starts: Vec<&Node> = node_map
        .values()
        .filter(|node| node.node_type == "start")
        .collect();
    let ends: Vec<&Node> = node_map
        .values()
        .filter(|node| node.node_type == "end")
        .collect();
    if starts.len() != 1 || ends.is_empty() {
        return Err("Workflow requires exactly one start and at least one end.".into());
    }
    let start_id = starts[0].id.clone();
    let mut parsed_edges = Vec::with_capacity(edges.len());
    let mut incoming: HashMap<String, Vec<Edge>> =
        node_map.keys().map(|id| (id.clone(), Vec::new())).collect();
    let mut outgoing = incoming.clone();
    let mut seen = HashSet::new();
    for edge in edges {
        let Some(edge_object) = edge.as_object() else {
            return Err("Every workflow edge must be an object.".into());
        };
        let source = edge_object
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("");
        let target = edge_object
            .get("target")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !node_map.contains_key(source) || !node_map.contains_key(target) {
            return Err("Workflow edge references an unknown node.".into());
        }
        if source == target || !seen.insert((source.to_string(), target.to_string())) {
            return Err("Self edges and duplicate edges are not allowed.".into());
        }
        let when = edge_object.get("when").and_then(Value::as_bool);
        if node_map[source].node_type == "condition" {
            if when.is_none()
                || !edge_object
                    .get("when")
                    .map(Value::is_boolean)
                    .unwrap_or(false)
            {
                return Err("Edges from condition nodes require a boolean when value.".into());
            }
        } else if edge_object.contains_key("when") {
            return Err("Only condition nodes can have conditional edges.".into());
        }
        let edge = Edge {
            source: source.to_string(),
            target: target.to_string(),
            when,
        };
        parsed_edges.push(edge.clone());
        incoming.get_mut(target).unwrap().push(edge.clone());
        outgoing.get_mut(source).unwrap().push(edge);
    }
    for (target, target_edges) in &incoming {
        if node_map[target].config.get("join").and_then(Value::as_str) != Some("all") {
            continue;
        }
        if target_edges.iter().any(|edge| edge.when.is_some()) {
            return Err(format!(
                "Node {target} join=all cannot consume conditional edges."
            ));
        }
    }
    if !incoming[&start_id].is_empty() {
        return Err("Start node cannot have incoming edges.".into());
    }
    for end in ends {
        if !outgoing[&end.id].is_empty() {
            return Err("End nodes cannot have outgoing edges.".into());
        }
    }
    for (node_id, node) in &node_map {
        if let Some(source) = source_reference(node) {
            if !node_map.contains_key(source) {
                return Err(format!("Node {node_id} references an unknown source node."));
            }
        }
        if node.node_type == "condition" {
            let branches: HashSet<Option<bool>> =
                outgoing[node_id].iter().map(|edge| edge.when).collect();
            if !(branches.contains(&Some(true)) && branches.contains(&Some(false))) {
                return Err(format!(
                    "Condition node {node_id} requires true and false outgoing branches."
                ));
            }
        }
        if node_id != &start_id && incoming[node_id].is_empty() {
            return Err(format!("Node {node_id} is disconnected from the start."));
        }
        if node.node_type != "end" && outgoing[node_id].is_empty() {
            return Err(format!(
                "Node {node_id} has no outgoing edge; every branch must finish at an end node."
            ));
        }
    }
    let mut indegree: HashMap<String, usize> = node_map
        .keys()
        .map(|id| (id.clone(), incoming[id].len()))
        .collect();
    let mut queue = VecDeque::from([start_id.clone()]);
    let mut order = Vec::new();
    while let Some(node_id) = queue.pop_front() {
        order.push(node_id.clone());
        for edge in &outgoing[&node_id] {
            let degree = indegree.get_mut(&edge.target).unwrap();
            *degree -= 1;
            if *degree == 0 {
                queue.push_back(edge.target.clone());
            }
        }
    }
    if order.len() != node_map.len() {
        return Err("Workflow graph contains a cycle.".into());
    }
    let order_index: HashMap<String, usize> = order
        .iter()
        .enumerate()
        .map(|(index, id)| (id.clone(), index))
        .collect();
    for (node_id, node) in &node_map {
        if let Some(source) = source_reference(node) {
            if order_index[source] >= order_index[node_id] {
                return Err(format!(
                    "Node {node_id} source reference must point to an earlier node."
                ));
            }
        }
        if let Some(gather) = node.config.get("gather_from").and_then(Value::as_array) {
            for gathered in gather.iter().filter_map(Value::as_str) {
                if order_index[gathered] >= order_index[node_id] {
                    return Err(format!(
                        "Node {node_id} gather_from must reference earlier nodes."
                    ));
                }
            }
        }
    }
    let mut reachable = HashSet::new();
    let mut pending = vec![start_id.clone()];
    while let Some(node_id) = pending.pop() {
        if !reachable.insert(node_id.clone()) {
            continue;
        }
        pending.extend(outgoing[&node_id].iter().map(|edge| edge.target.clone()));
    }
    if reachable.len() != node_map.len() {
        return Err("Every node must be reachable from the start node.".into());
    }
    Ok(Graph {
        nodes: node_map,
        edges: parsed_edges,
        incoming,
        outgoing,
        order,
        start_id,
    })
}

fn valid_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 120
        && value.chars().all(|character| {
            character.is_ascii_alphanumeric() || character == '_' || character == '-'
        })
}

fn source_reference(node: &Node) -> Option<&str> {
    node.config
        .get("input_from")
        .and_then(Value::as_str)
        .or_else(|| node.config.get("source_node").and_then(Value::as_str))
        .or_else(|| node.config.get("arguments_from").and_then(Value::as_str))
}

fn validate_config(
    node_id: &str,
    node_type: &str,
    config: &Map<String, Value>,
) -> Result<(), String> {
    let join = match config.get("join") {
        Some(Value::String(value)) => value.as_str(),
        Some(_) => return Err(format!("Node {node_id} join must be any or all.")),
        None => "any",
    };
    if join != "any" && join != "all" {
        return Err(format!("Node {node_id} join must be any or all."));
    }
    if node_type == "start" && join != "any" {
        return Err("Start node cannot declare a join strategy.".into());
    }
    if contains_secret(&Value::Object(config.clone())) {
        return Err(format!(
            "Node {node_id} config cannot contain credential material."
        ));
    }
    if node_type == "llm" {
        let model = config
            .get("model_config_id")
            .and_then(Value::as_str)
            .ok_or_else(|| format!("LLM node {node_id} requires model_config_id."))?;
        uuid::Uuid::parse_str(model)
            .map_err(|_| format!("LLM node {node_id} model_config_id must be a UUID."))?;
        let prompt = config
            .get("prompt_template")
            .and_then(Value::as_str)
            .ok_or_else(|| format!("LLM node {node_id} requires prompt_template."))?;
        if prompt.len() > 12000 {
            return Err(format!("LLM node {node_id} prompt is too long."));
        }
    }
    if node_type == "tool" {
        let capability = config
            .get("capability")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| format!("Tool node {node_id} requires capability."))?;
        let _ = capability;
        if let Some(arguments) = config.get("arguments") {
            if !arguments.is_object() {
                return Err(format!("Tool node {node_id} arguments must be an object."));
            }
        }
    }
    if node_type == "knowledge_search" {
        let ids = config
            .get("knowledge_base_ids")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                format!("Knowledge search node {node_id} requires 1 to 8 knowledge bases.")
            })?;
        if !(1..=8).contains(&ids.len()) {
            return Err(format!(
                "Knowledge search node {node_id} requires 1 to 8 knowledge bases."
            ));
        }
        for id in ids {
            let Some(id) = id.as_str() else {
                return Err(format!(
                    "Knowledge search node {node_id} contains an invalid knowledge base ID."
                ));
            };
            uuid::Uuid::parse_str(id).map_err(|_| {
                format!("Knowledge search node {node_id} contains an invalid knowledge base ID.")
            })?;
        }
        let unique: HashSet<&str> = ids.iter().filter_map(Value::as_str).collect();
        if unique.len() != ids.len() {
            return Err(format!(
                "Knowledge search node {node_id} cannot repeat knowledge bases."
            ));
        }
        let template = match config.get("query_template") {
            Some(Value::String(value)) => value.as_str(),
            Some(_) => {
                return Err(format!(
                    "Knowledge search node {node_id} query_template is invalid."
                ))
            }
            None => "{{input}}",
        };
        if template.is_empty() || template.len() > 4000 {
            return Err(format!(
                "Knowledge search node {node_id} query_template is invalid."
            ));
        }
        let limit = config.get("limit").cloned().unwrap_or(json!(5));
        let Some(limit) = limit.as_i64() else {
            return Err(format!(
                "Knowledge search node {node_id} limit must be 1 to 10."
            ));
        };
        if !(1..=10).contains(&limit) {
            return Err(format!(
                "Knowledge search node {node_id} limit must be 1 to 10."
            ));
        }
    }
    if node_type == "agent" {
        let version = config
            .get("agent_version_id")
            .and_then(Value::as_str)
            .ok_or_else(|| format!("Agent node {node_id} requires agent_version_id."))?;
        uuid::Uuid::parse_str(version)
            .map_err(|_| format!("Agent node {node_id} agent_version_id must be a UUID."))?;
        if let Some(capability) = config.get("capability") {
            if !capability.is_null() && !capability.is_string() {
                return Err(format!("Agent node {node_id} capability must be a string."));
            }
        }
        if let Some(arguments) = config.get("arguments") {
            if !arguments.is_null() && !arguments.is_object() {
                return Err(format!("Agent node {node_id} arguments must be an object."));
            }
        }
        let instructions = match config.get("instructions") {
            Some(Value::String(value)) => value.as_str(),
            Some(_) => return Err(format!("Agent node {node_id} instructions are invalid.")),
            None => "",
        };
        if instructions.len() > 4000 {
            return Err(format!("Agent node {node_id} instructions are invalid."));
        }
    }
    if let Some(gather) = config.get("gather_from") {
        let Some(gather) = gather.as_array() else {
            return Err(format!(
                "Node {node_id} gather_from must contain 1 to 12 nodes."
            ));
        };
        if !(1..=12).contains(&gather.len()) {
            return Err(format!(
                "Node {node_id} gather_from must contain 1 to 12 nodes."
            ));
        }
        let mut seen = HashSet::new();
        for item in gather {
            let Some(id) = item.as_str().filter(|id| valid_id(id)) else {
                return Err(format!(
                    "Node {node_id} gather_from contains an invalid node id."
                ));
            };
            if id == node_id || !seen.insert(id) {
                return Err(format!("Node {node_id} gather_from cannot repeat nodes."));
            }
        }
        if config
            .get("input_from")
            .map(|value| !value.is_null())
            .unwrap_or(false)
            || config
                .get("source_node")
                .map(|value| !value.is_null())
                .unwrap_or(false)
            || config
                .get("arguments_from")
                .map(|value| !value.is_null())
                .unwrap_or(false)
        {
            return Err(format!(
                "Node {node_id} cannot combine gather_from with a single source."
            ));
        }
    }
    if node_type == "condition" {
        if !config.contains_key("equals") {
            return Err(format!("Condition node {node_id} requires equals."));
        }
        if let Some(normalize) = config.get("normalize") {
            if !normalize.is_boolean() {
                return Err(format!(
                    "Condition node {node_id} normalize must be a boolean."
                ));
            }
        }
        let input_key = match config.get("input_key") {
            Some(Value::String(value)) => value.as_str(),
            Some(_) => {
                return Err(format!(
                    "Condition node {node_id} input_key must be a non-empty string."
                ))
            }
            None => "input",
        };
        if input_key.is_empty() || input_key.len() > 120 {
            return Err(format!(
                "Condition node {node_id} input_key must be a non-empty string."
            ));
        }
    }
    if node_type == "approval" {
        let instructions = match config.get("instructions") {
            Some(Value::String(value)) => value.as_str(),
            Some(_) => return Err(format!("Approval node {node_id} instructions are invalid.")),
            None => "",
        };
        if instructions.len() > 4000 {
            return Err(format!("Approval node {node_id} instructions are invalid."));
        }
    }
    if node_type == "loop" {
        let max_iterations = config.get("max_iterations").and_then(Value::as_i64);
        let Some(max_iterations) = max_iterations else {
            return Err(format!(
                "Loop node {node_id} max_iterations must be an integer from 2 to 10."
            ));
        };
        if !(2..=10).contains(&max_iterations) {
            return Err(format!(
                "Loop node {node_id} max_iterations must be an integer from 2 to 10."
            ));
        }
        let body = config
            .get("body")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("Loop node {node_id} requires an inline body object."))?;
        let body_type = body.get("type").and_then(Value::as_str).unwrap_or("");
        if !["llm", "tool", "knowledge_search", "agent"].contains(&body_type) {
            return Err(format!("Loop node {node_id} body type is not supported."));
        }
        let body_config = match body.get("config") {
            Some(Value::Object(config)) => config.clone(),
            Some(_) => {
                return Err(format!(
                    "Loop node {node_id} body config must be an object."
                ))
            }
            None => Map::new(),
        };
        if ["input_from", "source_node", "arguments_from", "gather_from"]
            .iter()
            .any(|key| body_config.contains_key(*key))
        {
            return Err(format!(
                "Loop node {node_id} body must use the iteration input."
            ));
        }
        if contains_secret(&Value::Object(body_config.clone())) {
            return Err(format!(
                "Loop node {node_id} body config cannot contain credential material."
            ));
        }
        let body_node_id = format!("{node_id}_body");
        validate(&json!({
            "nodes": [
                {"id": "start", "type": "start", "config": {}},
                {"id": body_node_id, "type": body_type, "config": body_config},
                {"id": "end", "type": "end", "config": {"source_node": body_node_id}}
            ],
            "edges": [
                {"source": "start", "target": body_node_id},
                {"source": body_node_id, "target": "end"}
            ]
        }))?;
    }
    if NODE_TYPES.contains(&node_type) && node_type != "start" {
        if let Some(source) = source_reference_value(config) {
            if !valid_id(source) || source == node_id {
                return Err(format!(
                    "Node {node_id} has an invalid source node reference."
                ));
            }
        }
    }
    Ok(())
}

fn source_reference_value(config: &Map<String, Value>) -> Option<&str> {
    config
        .get("input_from")
        .and_then(Value::as_str)
        .or_else(|| config.get("source_node").and_then(Value::as_str))
        .or_else(|| config.get("arguments_from").and_then(Value::as_str))
}

fn contains_secret(value: &Value) -> bool {
    match value {
        Value::Object(map) => map.iter().any(|(key, child)| {
            FORBIDDEN_CREDENTIAL_KEYS.contains(&key.to_lowercase().as_str())
                || contains_secret(child)
        }),
        Value::Array(values) => values.iter().any(contains_secret),
        _ => false,
    }
}

fn validate_ui(object: &Map<String, Value>, nodes: &HashMap<String, Node>) -> Result<(), String> {
    let Some(ui) = object.get("ui") else {
        return Ok(());
    };
    let Some(ui) = ui.as_object() else {
        return Err("Workflow ui metadata must be an object.".into());
    };
    let Some(positions) = ui.get("node_positions") else {
        return Ok(());
    };
    let Some(positions) = positions.as_object() else {
        return Err("Workflow ui.node_positions must be an object with at most 50 entries.".into());
    };
    if positions.len() > 50 {
        return Err("Workflow ui.node_positions must be an object with at most 50 entries.".into());
    }
    for (node_id, position) in positions {
        if !nodes.contains_key(node_id) {
            return Err(format!(
                "Workflow ui.node_positions references an unknown node: {node_id}"
            ));
        }
        let Some(position) = position.as_object() else {
            return Err(format!(
                "Workflow ui.node_positions[{node_id}] must be an object."
            ));
        };
        for axis in ["x", "y"] {
            let Some(value) = position.get(axis).and_then(Value::as_f64) else {
                return Err(format!(
                    "Workflow ui.node_positions[{node_id}].{axis} must be a number."
                ));
            };
            if !(0.0..=100000.0).contains(&value) {
                return Err(format!(
                    "Workflow ui.node_positions[{node_id}].{axis} is out of range."
                ));
            }
        }
    }
    Ok(())
}

pub fn execution_layers(graph: &Graph) -> Vec<Vec<String>> {
    let mut depths: HashMap<String, usize> = HashMap::new();
    let mut layers: Vec<Vec<String>> = Vec::new();
    for node_id in &graph.order {
        let depth = graph.incoming[node_id]
            .iter()
            .map(|edge| depths.get(&edge.source).copied().unwrap_or(0) + 1)
            .max()
            .unwrap_or(0);
        depths.insert(node_id.clone(), depth);
        if layers.len() <= depth {
            layers.resize(depth + 1, Vec::new());
        }
        layers[depth].push(node_id.clone());
    }
    layers
}

pub async fn validate_references(
    graph: &Graph,
    pool: &sqlx::AnyPool,
    postgres: bool,
    tenant_id: &str,
    project_id: &str,
) -> Result<(), String> {
    for node in graph.nodes.values() {
        validate_node_references(node, pool, postgres, tenant_id, project_id).await?;
    }
    Ok(())
}

async fn validate_node_references(
    node: &Node,
    pool: &sqlx::AnyPool,
    postgres: bool,
    tenant_id: &str,
    project_id: &str,
) -> Result<(), String> {
    if node.node_type == "loop" {
        let body = node.config.get("body").and_then(Value::as_object);
        if let Some(body) = body {
            let body_type = body.get("type").and_then(Value::as_str).unwrap_or("");
            let body_config = body
                .get("config")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let body_node = Node {
                id: format!("{}_body", node.id),
                node_type: body_type.to_string(),
                config: body_config,
            };
            Box::pin(validate_node_references(
                &body_node, pool, postgres, tenant_id, project_id,
            ))
            .await?;
        }
        return Ok(());
    }
    if node.node_type == "knowledge_search" {
        let ids = node
            .config
            .get("knowledge_base_ids")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        for id in ids.iter().filter_map(Value::as_str) {
            let capabilities = if postgres {
                "config.capabilities::text"
            } else {
                "config.capabilities"
            };
            let sql = format!(
                "SELECT {capabilities} AS capabilities, CAST(provider.tenant_id AS TEXT) AS provider_tenant \
                 FROM core_knowledgebase kb \
                 JOIN core_modelconfig config ON config.id = kb.embedding_model_id \
                 JOIN core_modelprovider provider ON provider.id = config.provider_id \
                 WHERE CAST(kb.id AS TEXT) = ? AND CAST(kb.tenant_id AS TEXT) = ? \
                   AND CAST(kb.project_id AS TEXT) = ? AND kb.is_archived = {} \
                   AND config.enabled = {} AND provider.enabled = {}",
                if postgres { "FALSE" } else { "0" },
                if postgres { "TRUE" } else { "1" },
                if postgres { "TRUE" } else { "1" },
            );
            let row = sqlx::query(&sql)
                .bind(id)
                .bind(tenant_id)
                .bind(project_id)
                .fetch_optional(pool)
                .await
                .map_err(|error| error.to_string())?;
            let Some(row) = row else {
                return Err("A workflow knowledge search node references an unavailable project knowledge base.".into());
            };
            let capabilities = capabilities_value(
                row.try_get::<Option<String>, _>("capabilities")
                    .ok()
                    .flatten(),
            );
            if !capabilities.iter().any(|value| value == "embeddings") {
                return Err("Workflow knowledge search models must support embeddings.".into());
            }
            let provider_tenant: Option<String> = row
                .try_get("provider_tenant")
                .map_err(|error| error.to_string())?;
            if provider_tenant
                .as_deref()
                .is_some_and(|value| value != tenant_id)
            {
                return Err("Workflow knowledge search models must support embeddings.".into());
            }
        }
        return Ok(());
    }
    if node.node_type == "tool" {
        let capability = node
            .config
            .get("capability")
            .and_then(Value::as_str)
            .unwrap_or("");
        let plugin_capabilities = if postgres {
            "plugin.capabilities::text"
        } else {
            "plugin.capabilities"
        };
        let granted = if postgres {
            "installation.granted_capabilities::text"
        } else {
            "installation.granted_capabilities"
        };
        let manifest = if postgres {
            "plugin.manifest::text"
        } else {
            "plugin.manifest"
        };
        let sql = format!(
            "SELECT {plugin_capabilities} AS capabilities, {granted} AS granted, \
             {manifest} AS manifest FROM core_agentplugininstallation installation \
             JOIN core_agentplugindefinition plugin ON plugin.id = installation.plugin_id \
             WHERE CAST(installation.tenant_id AS TEXT) = ? \
               AND CAST(installation.project_id AS TEXT) = ? \
               AND installation.status = 'installed' \
               AND plugin.enabled = {} AND installation.plugin_version = plugin.version",
            if postgres { "TRUE" } else { "1" }
        );
        let rows = sqlx::query(&sql)
            .bind(tenant_id)
            .bind(project_id)
            .fetch_all(pool)
            .await
            .map_err(|error| error.to_string())?;
        let mut resolved_risk: Option<String> = None;
        for row in &rows {
            let capabilities = capabilities_value(
                row.try_get::<Option<String>, _>("capabilities")
                    .ok()
                    .flatten(),
            );
            let granted_names: HashSet<String> = row
                .try_get::<Option<String>, _>("granted")
                .ok()
                .flatten()
                .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                .and_then(|value| {
                    value.as_array().map(|values| {
                        values
                            .iter()
                            .filter_map(Value::as_str)
                            .map(str::to_string)
                            .collect()
                    })
                })
                .unwrap_or_default();
            if !capabilities.iter().any(|value| value == capability)
                || !granted_names.contains(capability)
            {
                continue;
            }
            let manifest_value = row
                .try_get::<Option<String>, _>("manifest")
                .ok()
                .flatten()
                .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                .unwrap_or_else(|| json!({}));
            let declarations = manifest_value
                .get("capabilities")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let declaration = declarations
                .iter()
                .find(|entry| entry.get("name").and_then(Value::as_str) == Some(capability));
            let Some(declaration) = declaration else {
                continue;
            };
            resolved_risk = Some(capability_risk_level(declaration));
            break;
        }
        let Some(risk) = resolved_risk else {
            return Err(format!(
                "A workflow tool node references a capability that is not installed in this project: {capability}"
            ));
        };
        if ["high", "critical", "unclassified"].contains(&risk.as_str()) {
            return Err(
                "High-risk workflow capabilities require a human approval node and cannot execute directly."
                    .into(),
            );
        }
        return Ok(());
    }
    if node.node_type == "agent" {
        let version_id = node
            .config
            .get("agent_version_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let snapshot = if postgres {
            "version.config_snapshot::text"
        } else {
            "version.config_snapshot"
        };
        let sql = format!(
            "SELECT {snapshot} AS snapshot FROM core_agentversion version \
             JOIN core_agentdefinition agent ON agent.id = version.agent_id \
             WHERE CAST(version.id AS TEXT) = ? AND CAST(agent.tenant_id AS TEXT) = ? \
               AND CAST(agent.project_id AS TEXT) = ? AND agent.is_archived = {}",
            if postgres { "FALSE" } else { "0" }
        );
        let row = sqlx::query(&sql)
            .bind(version_id)
            .bind(tenant_id)
            .bind(project_id)
            .fetch_optional(pool)
            .await
            .map_err(|error| error.to_string())?;
        let Some(row) = row else {
            return Err("A workflow agent node references an unavailable agent version.".into());
        };
        let snapshot = row
            .try_get::<Option<String>, _>("snapshot")
            .ok()
            .flatten()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .unwrap_or_else(|| json!({}));
        if snapshot.get("kind").and_then(Value::as_str) == Some("marketplace_agent") {
            let capability = snapshot
                .get("capability")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty());
            let Some(capability) = capability else {
                return Err("A bound marketplace agent snapshot is missing its capability.".into());
            };
            if let Some(configured) = node.config.get("capability").and_then(Value::as_str) {
                if configured != capability {
                    return Err("Workflow agent node capability does not match the bound marketplace agent version.".into());
                }
            }
            return Ok(());
        }
        let Some(model_id) = snapshot.get("model_config_id").and_then(Value::as_str) else {
            return Err("A workflow agent node references an unavailable model.".into());
        };
        let model = model_row(pool, postgres, model_id).await?;
        let Some(model) = model else {
            return Err("A workflow agent node references an unavailable model.".into());
        };
        if model
            .provider_tenant
            .as_deref()
            .is_some_and(|value| value != tenant_id)
        {
            return Err("Workflow agent model provider is outside the tenant.".into());
        }
        if !model.capabilities.iter().any(|value| value == "chat") {
            return Err("Workflow agent node model must support chat.".into());
        }
        let knowledge_ids = snapshot
            .get("knowledge_base_ids")
            .cloned()
            .unwrap_or_else(|| json!([]));
        let Some(knowledge_ids) = knowledge_ids.as_array() else {
            return Err("Workflow agent knowledge base allowlist is invalid.".into());
        };
        if knowledge_ids.len() > 8 {
            return Err("Workflow agent knowledge base allowlist is invalid.".into());
        }
        let unique: HashSet<&str> = knowledge_ids.iter().filter_map(Value::as_str).collect();
        if unique.len() != knowledge_ids.len() {
            return Err("A workflow agent references an unavailable knowledge base.".into());
        }
        for id in unique {
            let false_value = if postgres { "FALSE" } else { "0" };
            let true_value = if postgres { "TRUE" } else { "1" };
            let sql = format!(
                "SELECT COUNT(*) FROM core_knowledgebase kb \
                 JOIN core_modelconfig config ON config.id = kb.embedding_model_id \
                 JOIN core_modelprovider provider ON provider.id = config.provider_id \
                 WHERE CAST(kb.id AS TEXT) = ? AND CAST(kb.tenant_id AS TEXT) = ? \
                   AND CAST(kb.project_id AS TEXT) = ? AND kb.is_archived = {false_value} \
                   AND config.enabled = {true_value} AND provider.enabled = {true_value}"
            );
            let available = sqlx::query_scalar::<_, i64>(&sql)
                .bind(id)
                .bind(tenant_id)
                .bind(project_id)
                .fetch_one(pool)
                .await
                .map_err(|error| error.to_string())?;
            if available == 0 {
                return Err("A workflow agent references an unavailable knowledge base.".into());
            }
        }
        return Ok(());
    }
    if node.node_type != "llm" {
        return Ok(());
    }
    let model_id = node
        .config
        .get("model_config_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let Some(model) = model_row(pool, postgres, model_id).await? else {
        return Err("A workflow LLM node references an unavailable model.".into());
    };
    let max_tokens = match node.config.get("max_tokens") {
        Some(Value::Number(value)) => value.as_i64().unwrap_or(-1),
        Some(_) => -1,
        None => model.max_output_tokens,
    };
    let timeout_seconds = match node.config.get("timeout_seconds") {
        Some(Value::Number(value)) => value.as_i64().unwrap_or(-1),
        Some(_) => -1,
        None => 60,
    };
    let system_prompt = match node.config.get("system_prompt") {
        Some(Value::String(value)) => value.as_str(),
        Some(_) => return Err("Workflow LLM system_prompt is invalid.".into()),
        None => "You are a helpful assistant.",
    };
    if system_prompt.len() > 12000 {
        return Err("Workflow LLM system_prompt is invalid.".into());
    }
    if max_tokens < 1 || max_tokens > model.max_output_tokens {
        return Err("Workflow LLM max_tokens exceeds model limits.".into());
    }
    if !(1..=120).contains(&timeout_seconds) {
        return Err("Workflow LLM timeout_seconds must be 1 to 120.".into());
    }
    if model
        .provider_tenant
        .as_deref()
        .is_some_and(|value| value != tenant_id)
    {
        return Err("Workflow model provider is outside the tenant.".into());
    }
    if !model.capabilities.iter().any(|value| value == "chat") {
        return Err("Workflow LLM node model must support chat.".into());
    }
    Ok(())
}

struct ModelRow {
    capabilities: Vec<String>,
    max_output_tokens: i64,
    provider_tenant: Option<String>,
}

async fn model_row(
    pool: &sqlx::AnyPool,
    postgres: bool,
    model_id: &str,
) -> Result<Option<ModelRow>, String> {
    let capabilities = if postgres {
        "config.capabilities::text"
    } else {
        "config.capabilities"
    };
    let sql = format!(
        "SELECT {capabilities} AS capabilities, config.max_output_tokens, \
         CAST(provider.tenant_id AS TEXT) AS provider_tenant \
         FROM core_modelconfig config JOIN core_modelprovider provider ON provider.id = config.provider_id \
         WHERE CAST(config.id AS TEXT) = ? AND config.enabled = {} AND provider.enabled = {}",
        if postgres { "TRUE" } else { "1" },
        if postgres { "TRUE" } else { "1" }
    );
    let row = sqlx::query(&sql)
        .bind(model_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    match row {
        Some(row) => {
            let provider_tenant = row
                .try_get::<Option<String>, _>("provider_tenant")
                .map_err(|error| error.to_string())?;
            Ok(Some(ModelRow {
                capabilities: capabilities_value(
                    row.try_get::<Option<String>, _>("capabilities")
                        .ok()
                        .flatten(),
                ),
                max_output_tokens: row.try_get("max_output_tokens").unwrap_or(0),
                provider_tenant,
            }))
        }
        None => Ok(None),
    }
}

fn capabilities_value(value: Option<String>) -> Vec<String> {
    value
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| {
            value.as_array().map(|values| {
                values
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
        })
        .unwrap_or_default()
}

fn capability_risk_level(declaration: &Value) -> String {
    match declaration.get("risk_level") {
        None => "low".to_string(),
        Some(Value::String(value))
            if ["low", "medium", "high", "critical", "unclassified"].contains(&value.as_str()) =>
        {
            value.clone()
        }
        Some(_) => "unclassified".to_string(),
    }
}

#[cfg(test)]
mod risk_tests {
    use super::*;

    #[test]
    fn unknown_capability_risk_values_fail_closed() {
        assert_eq!(
            capability_risk_level(&json!({"name": "read", "risk_level": "high"})),
            "high"
        );
        assert_eq!(
            capability_risk_level(&json!({"name": "read"})),
            "low"
        );
        assert_eq!(
            capability_risk_level(&json!({"name": "read", "risk_level": "unknown"})),
            "unclassified"
        );
        assert_eq!(
            capability_risk_level(&json!({"name": "read", "risk_level": 42})),
            "unclassified"
        );
    }
}
