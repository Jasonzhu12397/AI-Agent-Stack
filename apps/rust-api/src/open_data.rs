//! Built-in map and weather capabilities.
//!
//! The connector catalogue declares map and weather as built-in capabilities,
//! so they work in any deployment without an operator-supplied endpoint or key:
//! place lookup uses the Open-Meteo geocoder, route planning the OSRM router
//! and weather wttr.in. Every failure is a typed connector error the caller can
//! report in its own envelope.

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

const DEFAULT_GEOCODE_URL: &str = "https://geocoding-api.open-meteo.com/v1/search";
const DEFAULT_OSRM_URL: &str = "https://router.project-osrm.org";
const DEFAULT_WEATHER_URL: &str = "https://wttr.in";
const DEFAULT_USER_AGENT: &str =
    "ai-agent-stack-connector/1.0 (+https://github.com/Jasonzhu12397/AI-Agent-Stack)";
const FETCH_ATTEMPTS: usize = 3;
const FETCH_BACKOFF: Duration = Duration::from_millis(300);
const GEOCODE_TTL: Duration = Duration::from_secs(600);
const GEOCODE_CANDIDATES: usize = 8;
const MAX_DAYS: i64 = 7;

#[derive(Debug)]
pub struct ConnectorError {
    pub code: &'static str,
    pub message: String,
    pub status: u16,
}

impl ConnectorError {
    fn invalid(message: impl Into<String>) -> Self {
        Self {
            code: "INVALID_ARGUMENTS",
            message: message.into(),
            status: 400,
        }
    }

    fn not_found(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            status: 404,
        }
    }

    fn upstream(message: impl Into<String>) -> Self {
        Self {
            code: "UPSTREAM_FAILED",
            message: message.into(),
            status: 502,
        }
    }
}

/// Run one built-in capability; an unknown name is reported as such.
pub async fn invoke(capability: &str, arguments: &Value) -> Result<Value, ConnectorError> {
    let params = flatten(arguments);
    match capability {
        "map.place.search" => place_search(&params).await,
        "map.route.plan" => route(&params).await,
        "weather.current" => weather(&params, false).await,
        "weather.forecast" => weather(&params, true).await,
        _ => Err(ConnectorError::invalid(format!(
            "{capability} is not a built-in capability."
        ))),
    }
}

/// Arguments arrive as free-form JSON: keep the first value of each key as text,
/// exactly as the platform adapter did.
fn flatten(arguments: &Value) -> HashMap<String, String> {
    let mut params = HashMap::new();
    if let Some(object) = arguments.as_object() {
        for (key, value) in object {
            match value {
                Value::Null => continue,
                Value::Array(items) => {
                    if let Some(first) = items.first() {
                        params.insert(key.clone(), scalar(first));
                    }
                }
                Value::String(text) => {
                    params.insert(key.clone(), text.clone());
                }
                other => {
                    params.insert(key.clone(), scalar(other));
                }
            }
        }
    }
    params
}

fn scalar(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Bool(flag) => flag.to_string(),
        Value::Number(number) => number.to_string(),
        other => other.to_string(),
    }
}

fn text(
    params: &HashMap<String, String>,
    key: &str,
    required: bool,
) -> Result<String, ConnectorError> {
    let value = params
        .get(key)
        .map(|value| value.trim().to_string())
        .unwrap_or_default();
    if required && value.is_empty() {
        return Err(ConnectorError::invalid(format!("{key} is required.")));
    }
    Ok(value)
}

fn coordinate(params: &HashMap<String, String>, key: &str) -> Result<f64, ConnectorError> {
    let raw = text(params, key, true)?;
    raw.parse::<f64>()
        .map_err(|_| ConnectorError::invalid(format!("{key} must be a number.")))
}

fn as_float(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.trim().parse::<f64>().ok(),
        _ => None,
    }
}

fn as_int(value: Option<&Value>) -> Option<i64> {
    as_float(value).map(|number| number as i64)
}

fn env_or(name: &str, default: &str) -> String {
    match std::env::var(name) {
        Ok(value) if !value.trim().is_empty() => value,
        _ => default.to_string(),
    }
}

fn upstream_timeout() -> Duration {
    let seconds = std::env::var("CONNECTOR_UPSTREAM_TIMEOUT")
        .ok()
        .and_then(|raw| raw.parse::<f64>().ok())
        .filter(|value| *value > 0.0)
        .unwrap_or(12.0);
    Duration::from_secs_f64(seconds)
}

/// Fetch JSON with the platform's short retry: these public services answer
/// 503 for a burst from one datacenter IP, and a demo turn should survive it.
async fn fetch_json(url: &str, params: &[(&str, String)]) -> Result<Value, ConnectorError> {
    let client = reqwest::Client::builder()
        .timeout(upstream_timeout())
        .user_agent(env_or("CONNECTOR_USER_AGENT", DEFAULT_USER_AGENT))
        .build()
        .map_err(|error| ConnectorError::upstream(format!("HTTP client unavailable: {error}")))?;
    let mut last_error = String::from("no attempt");
    for attempt in 0..FETCH_ATTEMPTS {
        let response = client.get(url).query(params).send().await;
        match response {
            Ok(response) => {
                let status = response.status().as_u16();
                if status < 500 {
                    match response.text().await {
                        Ok(body) => match serde_json::from_str::<Value>(&body) {
                            Ok(value) => return Ok(value),
                            Err(error) => {
                                return Err(ConnectorError::upstream(format!(
                                    "Upstream returned invalid JSON: {error}"
                                )))
                            }
                        },
                        Err(error) => {
                            return Err(ConnectorError::upstream(format!(
                                "Upstream body could not be read: {error}"
                            )))
                        }
                    }
                }
                last_error = format!("HTTP {status}");
            }
            Err(error) => last_error = error.to_string(),
        }
        if attempt + 1 < FETCH_ATTEMPTS {
            tokio::time::sleep(FETCH_BACKOFF * ((attempt + 1) as u32)).await;
        }
    }
    Err(ConnectorError::upstream(format!(
        "Upstream request failed: {last_error}"
    )))
}

fn place_from(row: &Value, fallback: &str, kind_key: &str) -> Value {
    let name = row
        .get("name")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or(fallback);
    let mut place = Map::new();
    place.insert("name".into(), json!(name));
    for key in ["admin1", "country"] {
        if let Some(value) = row
            .get(key)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        {
            place.insert(key.into(), json!(value));
        }
    }
    if let Some(value) = row
        .get(kind_key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        place.insert("kind".into(), json!(value));
    }
    for key in ["latitude", "longitude"] {
        if let Some(number) = as_float(row.get(key)) {
            place.insert(key.into(), json!(number));
        }
    }
    Value::Object(place)
}

const FILLER_PREFIXES: [&str; 14] = [
    "请",
    "帮我",
    "帮忙",
    "麻烦",
    "我想",
    "想",
    "查一下",
    "查查",
    "查询",
    "查",
    "看一下",
    "看看",
    "看",
    "告诉我",
];
const FILLER_TAILS: [&str; 16] = [
    "天气",
    "气温",
    "温度",
    "预报",
    "怎么样",
    "怎样",
    "如何",
    "好不好",
    "咋样",
    "呢",
    "吗",
    "啊",
    "吧",
    "？",
    "?",
    "。",
];

fn strip_fillers(query: &str) -> String {
    let mut value = query.trim().to_string();
    for _ in 0..3 {
        let before = value.clone();
        for prefix in FILLER_PREFIXES {
            if let Some(rest) = value.strip_prefix(prefix) {
                value = rest.to_string();
            }
        }
        // Repeated short suffixes ("天气怎么样") peel off in one pass.
        let mut changed = true;
        while changed {
            changed = false;
            for tail in FILLER_TAILS {
                if let Some(rest) = value.strip_suffix(tail) {
                    value = rest.to_string();
                    changed = true;
                }
            }
            for tail in ["！", "!"] {
                if let Some(rest) = value.strip_suffix(tail) {
                    value = rest.to_string();
                    changed = true;
                }
            }
        }
        if value == before {
            break;
        }
    }
    value.trim().to_string()
}

/// The query itself, then progressively shorter windows: a chat turn arrives
/// as a sentence, and the whole sentence never resolves in a geocoder.
fn place_candidates(query: &str) -> Vec<String> {
    let value = query.trim();
    let mut candidates = vec![value.to_string()];
    let stripped = strip_fillers(value);
    if !stripped.is_empty() && stripped != value {
        candidates.push(stripped);
    }
    if value.chars().count() <= 3 {
        return candidates;
    }
    let chars: Vec<char> = value.chars().collect();
    for end in 2..=chars.len() {
        let prefix: String = chars[..end].iter().collect();
        let prefix = prefix.trim().to_string();
        if !prefix.is_empty() && !candidates.contains(&prefix) {
            candidates.push(prefix);
        }
        if candidates.len() >= GEOCODE_CANDIDATES {
            return candidates;
        }
    }
    for start in 1..chars.len().saturating_sub(1) {
        let suffix: String = chars[start..].iter().collect();
        let suffix = suffix.trim().to_string();
        if !suffix.is_empty() && !candidates.contains(&suffix) {
            candidates.push(suffix);
        }
        if candidates.len() >= GEOCODE_CANDIDATES {
            return candidates;
        }
    }
    candidates
}

async fn geocode_rows(place: &str, count: i64) -> Result<(Vec<Value>, String), ConnectorError> {
    let url = env_or("GEOCODE_URL", DEFAULT_GEOCODE_URL);
    let mut rows = Vec::new();
    let mut matched = place.to_string();
    for candidate in place_candidates(place) {
        let body = fetch_json(
            &url,
            &[
                ("name", candidate.clone()),
                ("count", count.to_string()),
                ("language", "zh".to_string()),
                ("format", "json".to_string()),
            ],
        )
        .await?;
        rows = body
            .get("results")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if !rows.is_empty() {
            matched = candidate;
            break;
        }
    }
    Ok((rows, matched))
}

fn geocode_cache() -> &'static Mutex<HashMap<String, (Instant, Value)>> {
    static CACHE: OnceLock<Mutex<HashMap<String, (Instant, Value)>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

async fn geocode(place: &str) -> Result<Value, ConnectorError> {
    if let Some((stamp, value)) = geocode_cache()
        .lock()
        .ok()
        .and_then(|cache| cache.get(place).cloned())
    {
        if stamp.elapsed() < GEOCODE_TTL {
            return Ok(value);
        }
    }
    let (rows, matched) = geocode_rows(place, 1).await?;
    let Some(row) = rows.first() else {
        return Err(ConnectorError::not_found(
            "PLACE_NOT_FOUND",
            format!("No place matched {matched:?}."),
        ));
    };
    let mut resolved = place_from(
        &json!({
            "name": row.get("name"),
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "kind": row.get("feature_code"),
            "admin1": row.get("admin1"),
            "country": row.get("country"),
        }),
        &matched,
        "kind",
    );
    if let Value::Object(ref mut map) = resolved {
        if map.get("name").is_none() {
            map.insert("name".into(), json!(matched));
        }
    }
    if let Ok(mut cache) = geocode_cache().lock() {
        cache.insert(place.to_string(), (Instant::now(), resolved.clone()));
    }
    Ok(resolved)
}

async fn place_search(params: &HashMap<String, String>) -> Result<Value, ConnectorError> {
    let query = text(params, "query", true)?;
    let limit = text(params, "limit", false)?
        .parse::<f64>()
        .ok()
        .map(|value| value as i64)
        .unwrap_or(5)
        .clamp(1, 10);
    let (rows, query) = geocode_rows(&query, limit).await?;
    if rows.is_empty() {
        return Err(ConnectorError::not_found(
            "PLACE_NOT_FOUND",
            format!("No place matched {query:?}."),
        ));
    }
    let places: Vec<Value> = rows
        .iter()
        .map(|row| {
            place_from(
                &json!({
                    "name": row.get("name"),
                    "latitude": row.get("latitude"),
                    "longitude": row.get("longitude"),
                    "kind": row.get("feature_code"),
                    "admin1": row.get("admin1"),
                    "country": row.get("country"),
                }),
                &query,
                "kind",
            )
        })
        .collect();
    Ok(json!({"ok": true, "query": query, "count": places.len(), "places": places}))
}

const ROUTE_TAILS: [&str; 8] = [
    "怎么走",
    "怎么去",
    "如何走",
    "路线",
    "多远",
    "多久",
    "开车",
    "步行",
];

fn route_endpoint(value: &str, role: &str) -> String {
    let mut text = strip_fillers(value);
    loop {
        let mut trimmed = false;
        for tail in ROUTE_TAILS {
            if let Some(rest) = text.strip_suffix(tail) {
                text = rest.to_string();
                trimmed = true;
            }
        }
        if let Some(rest) = text.strip_suffix("骑行") {
            text = rest.to_string();
            trimmed = true;
        }
        if !trimmed {
            break;
        }
    }
    if let Some((head, tail)) = text.split_once('到') {
        text = if role == "origin" { head } else { tail }.to_string();
    }
    let text = text.trim().to_string();
    if text.is_empty() {
        value.to_string()
    } else {
        text
    }
}

async fn route(params: &HashMap<String, String>) -> Result<Value, ConnectorError> {
    let origin = geocode(&route_endpoint(&text(params, "origin", true)?, "origin")).await?;
    let destination = geocode(&route_endpoint(
        &text(params, "destination", true)?,
        "destination",
    ))
    .await?;
    let mode = match text(params, "mode", false)? {
        value if value.is_empty() => "driving".to_string(),
        value => value,
    };
    if !matches!(mode.as_str(), "driving" | "walking" | "cycling") {
        return Err(ConnectorError::invalid(
            "mode must be driving, walking or cycling.",
        ));
    }
    let coordinates = format!(
        "{},{};{},{}",
        origin
            .get("longitude")
            .and_then(Value::as_f64)
            .unwrap_or(0.0),
        origin
            .get("latitude")
            .and_then(Value::as_f64)
            .unwrap_or(0.0),
        destination
            .get("longitude")
            .and_then(Value::as_f64)
            .unwrap_or(0.0),
        destination
            .get("latitude")
            .and_then(Value::as_f64)
            .unwrap_or(0.0)
    );
    let base = env_or("OSRM_URL", DEFAULT_OSRM_URL);
    let body = fetch_json(
        &format!("{base}/route/v1/{mode}/{coordinates}"),
        &[
            ("overview", "false".to_string()),
            ("steps", "false".to_string()),
        ],
    )
    .await?;
    let route = body
        .get("routes")
        .and_then(Value::as_array)
        .and_then(|routes| routes.first())
        .cloned()
        .ok_or_else(|| {
            ConnectorError::not_found("ROUTE_NOT_FOUND", "No route between those places.")
        })?;
    let distance = route.get("distance").and_then(Value::as_f64).unwrap_or(0.0);
    let duration = route.get("duration").and_then(Value::as_f64).unwrap_or(0.0);
    Ok(json!({
        "ok": true,
        "mode": mode,
        "origin": origin,
        "destination": destination,
        "distance_km": (distance / 1000.0 * 10.0).round() / 10.0,
        "duration_minutes": (duration / 60.0).round() as i64,
    }))
}

async fn weather(
    params: &HashMap<String, String>,
    forecast: bool,
) -> Result<Value, ConnectorError> {
    let place_name = text(params, "place", false)?;
    let resolved = if place_name.is_empty() {
        None
    } else {
        Some(geocode(&place_name).await?)
    };
    let (latitude, longitude) = match resolved.as_ref() {
        Some(place) => (
            place.get("latitude").and_then(Value::as_f64).unwrap_or(0.0),
            place
                .get("longitude")
                .and_then(Value::as_f64)
                .unwrap_or(0.0),
        ),
        None => (
            coordinate(params, "latitude")?,
            coordinate(params, "longitude")?,
        ),
    };
    let target = if resolved.is_some() {
        String::new()
    } else {
        place_name.clone()
    };
    let base = env_or("WEATHER_API_URL", DEFAULT_WEATHER_URL);
    let url = if target.is_empty() {
        format!("{base}/{latitude:.4},{longitude:.4}")
    } else {
        format!("{base}/{}", urlencode(&target))
    };
    let body = fetch_json(
        &url,
        &[("format", "j1".to_string()), ("lang", "zh".to_string())],
    )
    .await?;
    if body
        .get("current_condition")
        .and_then(Value::as_array)
        .map(Vec::is_empty)
        .unwrap_or(true)
    {
        return Err(ConnectorError::upstream("Weather response was empty."));
    }
    let area = body
        .get("nearest_area")
        .and_then(Value::as_array)
        .and_then(|areas| areas.first())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let area_value = |key: &str| -> Option<String> {
        area.get(key)
            .and_then(Value::as_array)
            .and_then(|items| items.first())
            .and_then(|item| item.get("value"))
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    let fallback = resolved
        .as_ref()
        .and_then(|place| place.get("name"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| {
            if place_name.is_empty() {
                format!("{latitude:.3},{longitude:.3}")
            } else {
                place_name.clone()
            }
        });
    let place = place_from(
        &json!({
            "name": fallback,
            "admin1": area_value("areaName"),
            "country": area_value("country"),
        }),
        &fallback,
        "kind",
    );
    if forecast {
        let days = text(params, "days", false)?
            .parse::<f64>()
            .ok()
            .map(|value| value as i64)
            .unwrap_or(3)
            .clamp(1, MAX_DAYS);
        let rows: Vec<Value> = body
            .get("weather")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .take(days as usize)
            .map(|row| {
                let hourly: Vec<Value> = row
                    .get("hourly")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let middle = hourly.get(hourly.len() / 2);
                let description = middle
                    .and_then(|hour| hour.get("weatherDesc"))
                    .and_then(Value::as_array)
                    .and_then(|items| items.first())
                    .and_then(|item| item.get("value"))
                    .and_then(Value::as_str);
                let precipitation = hourly
                    .iter()
                    .map(|hour| as_int(hour.get("chanceofrain")).unwrap_or(0))
                    .max()
                    .unwrap_or(0);
                let mut item = Map::new();
                item.insert(
                    "date".into(),
                    json!(row.get("date").and_then(Value::as_str).unwrap_or("")),
                );
                item.insert(
                    "temperature_max_c".into(),
                    json!(as_float(row.get("maxtempC"))),
                );
                item.insert(
                    "temperature_min_c".into(),
                    json!(as_float(row.get("mintempC"))),
                );
                item.insert("description".into(), json!(description));
                item.insert("precipitation_probability".into(), json!(precipitation));
                Value::Object(item)
            })
            .collect();
        return Ok(json!({"ok": true, "place": place, "days": rows}));
    }
    let current = body
        .get("current_condition")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let mut payload = Map::new();
    payload.insert("ok".into(), json!(true));
    payload.insert("place".into(), place);
    if let Some(observed) = current
        .get("localObsDateTime")
        .or_else(|| current.get("observation_time"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        payload.insert("observed_at".into(), json!(observed));
    }
    for (key, source) in [
        ("temperature_c", "temp_C"),
        ("apparent_temperature_c", "FeelsLikeC"),
        ("humidity_percent", "humidity"),
        ("wind_kmh", "windspeedKmph"),
    ] {
        if let Some(number) = as_float(current.get(source)) {
            payload.insert(key.into(), json!(number));
        }
    }
    if let Some(description) = current
        .get("weatherDesc")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .and_then(|item| item.get("value"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        payload.insert("description".into(), json!(description));
    }
    Ok(Value::Object(payload))
}

fn urlencode(value: &str) -> String {
    let mut out = String::new();
    for byte in value.as_bytes() {
        match *byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*byte as char)
            }
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_chat_fillers_from_a_place_query() {
        assert_eq!(strip_fillers("帮我查一下西安天气怎么样"), "西安");
        assert_eq!(strip_fillers("西安"), "西安");
    }

    #[test]
    fn pulls_the_origin_and_destination_out_of_an_instruction() {
        assert_eq!(route_endpoint("西安到咸阳怎么走", "origin"), "西安");
        assert_eq!(route_endpoint("西安到咸阳怎么走", "destination"), "咸阳");
    }

    #[tokio::test]
    #[ignore = "reaches the public geocoder, router and weather services"]
    async fn answers_the_public_services() {
        let places = invoke(
            "map.place.search",
            &json!({"query": "帮我查一下西安天气", "limit": 3}),
        )
        .await
        .unwrap();
        assert!(places["count"].as_i64().unwrap_or(0) >= 1);
        let route = invoke(
            "map.route.plan",
            &json!({"origin": "西安到咸阳怎么走", "destination": "西安到咸阳怎么走"}),
        )
        .await
        .unwrap();
        assert!(route["distance_km"].as_f64().unwrap_or(0.0) > 0.0);
        let current = invoke("weather.current", &json!({"place": "西安"}))
            .await
            .unwrap();
        assert!(current.get("temperature_c").is_some());
    }

    #[test]
    fn candidate_windows_start_with_the_whole_query() {
        let candidates = place_candidates("西安天气");
        assert_eq!(candidates.first().map(String::as_str), Some("西安天气"));
        assert!(candidates.iter().any(|value| value == "西安"));
    }
}
