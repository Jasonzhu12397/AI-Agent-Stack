#![allow(clippy::result_large_err)]

use std::collections::{BTreeSet, HashSet};
use std::net::Ipv4Addr;

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Map, Value};
use sqlx::Row;

use crate::proxy::AppState;
use crate::tenancy::project_access;

const ROLES: [&str; 6] = [
    "control",
    "data",
    "storage-internal",
    "storage-external",
    "sriov",
    "provisioning",
];
const HOST_ROLES: [&str; 3] = ["control_plane", "worker", "storage"];

#[derive(Clone)]
struct Network {
    index: i64,
    name: String,
    role: String,
    enabled: bool,
    vlan: Option<i64>,
    mtu: i64,
    dhcp: bool,
    default_route: bool,
    cidr: Option<String>,
    gateway: Option<String>,
    ip_pools: Vec<Value>,
    bond: Option<Value>,
    interface: Option<String>,
    pci_addresses: Vec<String>,
    num_vfs: i64,
    host_roles: Vec<String>,
}

fn fail(code: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({"detail": code}))).into_response()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "Database is not configured."})),
    )
        .into_response()
}

fn db_error(error: impl std::fmt::Display) -> Response {
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({"detail": format!("Database error: {error}")})),
    )
        .into_response()
}

fn not_found() -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({"detail": "Project not found."})),
    )
        .into_response()
}

fn is_name(value: &str) -> bool {
    if value.is_empty() || value.len() > 63 {
        return false;
    }
    let first = value.chars().next().unwrap_or(' ');
    let last = value.chars().last().unwrap_or(' ');
    (first.is_ascii_lowercase() || first.is_ascii_digit())
        && (last.is_ascii_lowercase() || last.is_ascii_digit())
        && value
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || matches!(ch, '-' | '_'))
}

fn is_interface(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | ':' | '@' | '-'))
}

fn is_pci(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 12
        && bytes[4] == b':'
        && bytes[7] == b':'
        && bytes[10] == b'.'
        && value.chars().enumerate().all(|(index, ch)| match index {
            4 | 7 => ch == ':',
            10 => ch == '.',
            11 => ch.is_ascii_digit() && ch <= '7',
            _ => ch.is_ascii_hexdigit(),
        })
}

fn parse_cidr(value: &str) -> Option<(Ipv4Addr, u8)> {
    let (address, prefix) = value.split_once('/')?;
    let address: Ipv4Addr = address.parse().ok()?;
    let prefix: u8 = prefix.parse().ok()?;
    if !(8..=32).contains(&prefix) {
        return None;
    }
    let mask = if prefix == 0 {
        0
    } else {
        u32::MAX << (32 - prefix)
    };
    let number = u32::from(address);
    if number & mask != number {
        return None;
    }
    Some((address, prefix))
}

fn ip_in_cidr(ip: Ipv4Addr, cidr: (Ipv4Addr, u8)) -> bool {
    let mask = if cidr.1 == 0 {
        0
    } else {
        u32::MAX << (32 - cidr.1)
    };
    u32::from(ip) & mask == u32::from(cidr.0)
}

fn overlaps(left: (Ipv4Addr, u8), right: (Ipv4Addr, u8)) -> bool {
    let left_end = u32::from(left.0)
        | if left.1 == 32 {
            0
        } else {
            (1u32 << (32 - left.1)) - 1
        };
    let right_end = u32::from(right.0)
        | if right.1 == 32 {
            0
        } else {
            (1u32 << (32 - right.1)) - 1
        };
    u32::from(left.0) <= right_end && u32::from(right.0) <= left_end
}

fn dns_name(name: &str) -> Result<String, String> {
    let mut text = String::new();
    for ch in name.trim().to_lowercase().chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-' {
            text.push(ch);
        } else {
            text.push('-');
        }
    }
    while text.contains("--") {
        text = text.replace("--", "-");
    }
    let text = text
        .trim_matches('-')
        .chars()
        .take(63)
        .collect::<String>()
        .trim_end_matches('-')
        .to_string();
    if !is_name(&text) || text.starts_with('-') || text.ends_with('-') {
        return Err("INFRA_NETWORK_NAME_INVALID".into());
    }
    Ok(text)
}

fn parse_network(value: &Value, index: i64) -> Result<Network, String> {
    let Some(object) = value.as_object() else {
        return Err("INFRA_NETWORK_INVALID".into());
    };
    let name = object.get("name").and_then(Value::as_str).unwrap_or("");
    if !is_name(name) {
        return Err("INFRA_NETWORK_NAME_INVALID".into());
    }
    let role = object.get("role").and_then(Value::as_str).unwrap_or("");
    if !ROLES.contains(&role) {
        return Err("INFRA_NETWORK_ROLE_INVALID".into());
    }
    let enabled = match object.get("enabled") {
        None => true,
        Some(Value::Bool(value)) => *value,
        Some(_) => return Err("INFRA_NETWORK_ENABLED_INVALID".into()),
    };
    let vlan = match object.get("vlan") {
        None | Some(Value::Null) => None,
        Some(Value::Number(value))
            if value
                .as_i64()
                .map(|number| (1..=4094).contains(&number))
                .unwrap_or(false) =>
        {
            value.as_i64()
        }
        Some(_) => return Err("INFRA_NETWORK_VLAN_INVALID".into()),
    };
    let mtu = match object.get("mtu") {
        None => 1500,
        Some(Value::Number(value))
            if value
                .as_i64()
                .map(|number| (576..=9216).contains(&number))
                .unwrap_or(false) =>
        {
            value.as_i64().unwrap_or(1500)
        }
        Some(_) => return Err("INFRA_NETWORK_MTU_INVALID".into()),
    };
    let dhcp = match object.get("dhcp") {
        None => false,
        Some(Value::Bool(value)) => *value,
        Some(_) => return Err("INFRA_NETWORK_DHCP_INVALID".into()),
    };
    let default_route = match object.get("default_route") {
        None => false,
        Some(Value::Bool(value)) => *value,
        Some(_) => return Err("INFRA_NETWORK_DEFAULT_ROUTE_INVALID".into()),
    };
    let cidr_value = object.get("cidr").and_then(Value::as_str).unwrap_or("");
    let cidr = if cidr_value.is_empty() {
        None
    } else {
        let parsed =
            parse_cidr(cidr_value).ok_or_else(|| "INFRA_NETWORK_CIDR_INVALID".to_string())?;
        Some((cidr_value.to_string(), parsed))
    };
    let gateway = match object.get("gateway_ipv4") {
        None | Some(Value::Null) => None,
        Some(Value::String(value)) if value.is_empty() => None,
        Some(Value::String(value)) => {
            let address: Ipv4Addr = value
                .parse()
                .map_err(|_| "INFRA_NETWORK_GATEWAY_INVALID".to_string())?;
            if let Some((_, cidr)) = cidr {
                if !ip_in_cidr(address, cidr) {
                    return Err("INFRA_NETWORK_GATEWAY_OUTSIDE_CIDR".into());
                }
            }
            Some(address.to_string())
        }
        Some(_) => return Err("INFRA_NETWORK_GATEWAY_INVALID".into()),
    };
    if cidr.is_none() && !dhcp && role != "sriov" {
        return Err("INFRA_NETWORK_CIDR_REQUIRED".into());
    }
    let ip_pools = if let Some((_, network)) = cidr {
        parse_pools(object.get("ip_pools"), network)?
    } else {
        Vec::new()
    };
    if cidr.is_some() && !dhcp && ip_pools.is_empty() && role != "sriov" {
        return Err("INFRA_NETWORK_IP_POOL_REQUIRED".into());
    }
    let pci_addresses = match object.get("pci_addresses") {
        None => Vec::new(),
        Some(Value::Array(items)) if items.len() <= 16 => {
            let mut values = Vec::new();
            for item in items {
                let Some(text) = item.as_str() else {
                    return Err("INFRA_NETWORK_PCI_ADDRESS_INVALID".into());
                };
                let lower = text.to_lowercase();
                if !is_pci(&lower) {
                    return Err("INFRA_NETWORK_PCI_ADDRESS_INVALID".into());
                }
                values.push(lower);
            }
            values
        }
        Some(_) => return Err("INFRA_NETWORK_PCI_ADDRESSES_INVALID".into()),
    };
    let bond = match object.get("bond") {
        None | Some(Value::Null) => None,
        Some(Value::Object(bond)) => {
            let bond_name = bond.get("name").and_then(Value::as_str).unwrap_or("");
            if !is_interface(bond_name) {
                return Err("INFRA_NETWORK_BOND_NAME_INVALID".into());
            }
            let mut interfaces: Vec<String> = match bond.get("interfaces") {
                Some(Value::Array(items)) => items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect(),
                None => Vec::new(),
                _ => return Err("INFRA_NETWORK_BOND_INTERFACES_INVALID".into()),
            };
            if interfaces.is_empty() && !pci_addresses.is_empty() {
                interfaces = pci_addresses
                    .iter()
                    .map(|address| format!("pci@{address}"))
                    .collect();
            }
            let unique: HashSet<&String> = interfaces.iter().collect();
            if interfaces.is_empty() || interfaces.len() > 8 || unique.len() != interfaces.len() {
                return Err("INFRA_NETWORK_BOND_INTERFACES_INVALID".into());
            }
            if interfaces.iter().any(|item| !is_interface(item)) {
                return Err("INFRA_NETWORK_BOND_INTERFACE_INVALID".into());
            }
            let mode = bond
                .get("mode")
                .and_then(Value::as_str)
                .unwrap_or("active-backup");
            if !["active-backup", "802.3ad", "balance-rr", "balance-xor"].contains(&mode) {
                return Err("INFRA_NETWORK_BOND_MODE_INVALID".into());
            }
            Some(json!({"name": bond_name, "interfaces": interfaces, "mode": mode}))
        }
        Some(_) => return Err("INFRA_NETWORK_BOND_INVALID".into()),
    };
    let mut direct_interface = None;
    if bond.is_none() {
        let mut declared = object
            .get("interface")
            .and_then(Value::as_str)
            .map(str::to_string);
        if let (Some(text), Some(vlan)) = (declared.as_mut(), vlan) {
            let suffix = format!(".{vlan}");
            if text.ends_with(&suffix) {
                let trimmed = text.trim_end_matches(&suffix).to_string();
                *text = trimmed;
            }
        }
        if declared.is_some() || vlan.is_none() {
            let value = declared.unwrap_or_default();
            if !is_interface(&value) {
                return Err("INFRA_NETWORK_INTERFACE_INVALID".into());
            }
            direct_interface = Some(value);
        }
    }
    let num_vfs = match object.get("num_vfs") {
        None => 8,
        Some(Value::Number(value))
            if value
                .as_i64()
                .map(|number| (1..=128).contains(&number))
                .unwrap_or(false) =>
        {
            value.as_i64().unwrap_or(8)
        }
        Some(_) => return Err("INFRA_NETWORK_NUM_VFS_INVALID".into()),
    };
    let host_roles: Vec<String> = match object.get("host_roles") {
        Some(Value::Array(items)) if !items.is_empty() => {
            let mut roles = Vec::new();
            for item in items {
                let Some(text) = item.as_str() else {
                    return Err("INFRA_NETWORK_HOST_ROLES_INVALID".into());
                };
                if !HOST_ROLES.contains(&text) || roles.iter().any(|existing| existing == text) {
                    return Err("INFRA_NETWORK_HOST_ROLES_INVALID".into());
                }
                roles.push(text.to_string());
            }
            roles
        }
        _ => return Err("INFRA_NETWORK_HOST_ROLES_INVALID".into()),
    };
    Ok(Network {
        index,
        name: name.into(),
        role: role.into(),
        enabled,
        vlan,
        mtu,
        dhcp,
        default_route,
        cidr: cidr.map(|(text, _)| text),
        gateway,
        ip_pools,
        bond,
        interface: direct_interface,
        pci_addresses,
        num_vfs,
        host_roles,
    })
}

fn parse_pools(value: Option<&Value>, network: (Ipv4Addr, u8)) -> Result<Vec<Value>, String> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let Some(items) = value.as_array() else {
        return Err("INFRA_NETWORK_IP_POOL_INVALID".into());
    };
    if items.is_empty() || items.len() > 8 {
        return Err("INFRA_NETWORK_IP_POOL_INVALID".into());
    }
    let mut pools = Vec::new();
    let mut ranges: Vec<(Ipv4Addr, Ipv4Addr)> = Vec::new();
    for item in items {
        let Some(object) = item.as_object() else {
            return Err("INFRA_NETWORK_IP_POOL_INVALID".into());
        };
        let start: Ipv4Addr = object
            .get("start")
            .and_then(Value::as_str)
            .unwrap_or("")
            .parse()
            .map_err(|_| "INFRA_NETWORK_IP_POOL_INVALID".to_string())?;
        let end: Ipv4Addr = object
            .get("end")
            .and_then(Value::as_str)
            .unwrap_or("")
            .parse()
            .map_err(|_| "INFRA_NETWORK_IP_POOL_INVALID".to_string())?;
        let prefix = match object.get("prefix") {
            None => network.1,
            Some(Value::Number(value)) => value
                .as_i64()
                .filter(|number| (1..=32).contains(number))
                .ok_or_else(|| "INFRA_NETWORK_IP_POOL_INVALID".to_string())?
                as u8,
            _ => return Err("INFRA_NETWORK_IP_POOL_INVALID".into()),
        };
        if start > end || !ip_in_cidr(start, network) || !ip_in_cidr(end, network) {
            return Err("INFRA_NETWORK_IP_POOL_INVALID".into());
        }
        if !(start.is_private() && end.is_private()) {
            return Err("INFRA_NETWORK_IP_POOL_INVALID".into());
        }
        for (left, right) in &ranges {
            if start <= *right && *left <= end {
                return Err("INFRA_NETWORK_IP_POOL_INVALID_OVERLAP".into());
            }
        }
        ranges.push((start, end));
        pools.push(json!({"start": start.to_string(), "end": end.to_string(), "prefix": prefix}));
    }
    Ok(pools)
}

fn plan(value: &Value) -> Result<Value, String> {
    let Some(object) = value.as_object() else {
        return Err("INFRA_NETWORK_PROFILE_INVALID".into());
    };
    let networks_value = object
        .get("networks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if networks_value.is_empty() || networks_value.len() > 64 {
        return Err("INFRA_NETWORK_PROFILE_NETWORKS_REQUIRED".into());
    }
    let dns: Vec<String> = match object.get("dns_servers") {
        None => Vec::new(),
        Some(Value::Array(items)) if items.len() <= 8 => {
            let mut values = Vec::new();
            for item in items {
                let Some(text) = item.as_str() else {
                    return Err("INFRA_NETWORK_DNS_INVALID".into());
                };
                if text.is_empty() {
                    return Err("INFRA_NETWORK_DNS_INVALID".into());
                }
                values.push(text.to_string());
            }
            values
        }
        _ => return Err("INFRA_NETWORK_DNS_INVALID".into()),
    };
    let mut names = HashSet::new();
    let mut parsed = Vec::new();
    for (index, item) in networks_value.iter().enumerate() {
        let network = parse_network(item, index as i64)?;
        if !network.enabled {
            continue;
        }
        if !names.insert(network.name.clone()) {
            return Err("INFRA_NETWORK_NAME_DUPLICATE".into());
        }
        parsed.push(network);
    }
    if parsed.is_empty() {
        return Err("INFRA_NETWORK_PROFILE_EMPTY".into());
    }
    let roles: BTreeSet<String> = parsed.iter().map(|network| network.role.clone()).collect();
    if !roles.contains("control") {
        return Err("INFRA_NETWORK_CONTROL_REQUIRED".into());
    }
    if !roles.contains("data") {
        return Err("INFRA_NETWORK_DATA_REQUIRED".into());
    }
    for role in ["control", "data", "storage-internal", "storage-external"] {
        if parsed.iter().filter(|network| network.role == role).count() > 1 {
            return Err("INFRA_NETWORK_ROLE_DUPLICATE".into());
        }
    }
    let cidrs: Vec<(Ipv4Addr, u8)> = parsed
        .iter()
        .filter_map(|network| {
            if network.role == "sriov" || network.role == "provisioning" {
                None
            } else {
                network.cidr.as_deref().and_then(parse_cidr)
            }
        })
        .collect();
    for left in 0..cidrs.len() {
        for right in left + 1..cidrs.len() {
            if overlaps(cidrs[left], cidrs[right]) {
                return Err("INFRA_NETWORK_CIDR_OVERLAP".into());
            }
        }
    }
    for host_role in HOST_ROLES {
        if !parsed
            .iter()
            .any(|network| network.host_roles.iter().any(|role| role == host_role))
        {
            return Err(format!(
                "INFRA_NETWORK_{}_ROLE_UNBOUND",
                host_role.to_uppercase()
            ));
        }
    }
    if !parsed.iter().any(|network| {
        network.role == "control"
            && network
                .host_roles
                .iter()
                .any(|role| role == "control_plane")
    }) {
        return Err("INFRA_NETWORK_CONTROL_PLANE_BINDING_REQUIRED".into());
    }
    let defaults: Vec<&Network> = parsed
        .iter()
        .filter(|network| network.default_route)
        .collect();
    if defaults.len() > 1 {
        return Err("INFRA_NETWORK_DEFAULT_ROUTE_DUPLICATE".into());
    }
    if let Some(default) = defaults.first() {
        if default.gateway.is_none() {
            return Err("INFRA_NETWORK_DEFAULT_ROUTE_GATEWAY_REQUIRED".into());
        }
        if default.role == "sriov" {
            return Err("INFRA_NETWORK_DEFAULT_ROUTE_ROLE_INVALID".into());
        }
    }
    let mut network_values = Vec::new();
    for network in &parsed {
        network_values.push(json!({
            "index": network.index,
            "name": network.name,
            "role": network.role,
            "enabled": network.enabled,
            "vlan": network.vlan,
            "mtu": network.mtu,
            "dhcp": network.dhcp,
            "default_route": network.default_route,
            "cidr": network.cidr,
            "gateway_ipv4": network.gateway,
            "ip_pools": network.ip_pools,
            "bond": network.bond,
            "interface": network.interface,
            "pci_addresses": network.pci_addresses,
            "num_vfs": network.num_vfs,
            "host_roles": network.host_roles,
        }));
    }
    Ok(json!({"networks": network_values, "dns_servers": dns}))
}

fn by_role<'a>(plan: &'a Value, role: &str) -> Option<&'a Value> {
    plan.get("networks")?
        .as_array()?
        .iter()
        .find(|network| network.get("role").and_then(Value::as_str) == Some(role))
}

fn vlan_base(plan: &Value, network: &Value) -> Result<String, String> {
    let role = network.get("role").and_then(Value::as_str).unwrap_or("");
    let preferred = if ["data", "storage-internal", "storage-external", "sriov"].contains(&role) {
        ["data", "control"]
    } else {
        ["control", "data"]
    };
    for candidate in preferred {
        let Some(base) = by_role(plan, candidate) else {
            continue;
        };
        if std::ptr::eq(base, network) || base.get("vlan").and_then(Value::as_i64).is_some() {
            continue;
        }
        if let Some(name) = base
            .get("bond")
            .and_then(|bond| bond.get("name"))
            .and_then(Value::as_str)
        {
            return Ok(name.into());
        }
        if let Some(name) = base.get("interface").and_then(Value::as_str) {
            return Ok(name.into());
        }
    }
    Err("INFRA_NETWORK_BASE_INTERFACE_REQUIRED".into())
}

fn link_id(plan: &Value, network: &Value) -> Result<String, String> {
    if let Some(vlan) = network.get("vlan").and_then(Value::as_i64) {
        let base = if let Some(name) = network
            .get("bond")
            .and_then(|bond| bond.get("name"))
            .and_then(Value::as_str)
        {
            name.to_string()
        } else if let Some(name) = network.get("interface").and_then(Value::as_str) {
            name.to_string()
        } else {
            vlan_base(plan, network)?
        };
        Ok(format!("{base}.{vlan}"))
    } else if let Some(name) = network
        .get("bond")
        .and_then(|bond| bond.get("name"))
        .and_then(Value::as_str)
    {
        Ok(name.into())
    } else {
        network
            .get("interface")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| "INFRA_NETWORK_INTERFACE_INVALID".into())
    }
}

fn base_link(plan: &Value, base: &str) -> Option<Value> {
    for network in plan.get("networks")?.as_array()? {
        if let Some(bond) = network.get("bond") {
            if bond.get("name").and_then(Value::as_str) == Some(base) {
                return Some(bond.clone());
            }
        }
        if network.get("interface").and_then(Value::as_str) == Some(base) {
            return Some(json!({"name": base, "interfaces": [base], "mode": ""}));
        }
    }
    None
}

fn role_links(plan: &Value, host_role: &str) -> Result<Value, String> {
    let mut ethernets = Vec::new();
    let mut bonds = Vec::new();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    for network in plan
        .get("networks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        if !network
            .get("host_roles")
            .and_then(Value::as_array)
            .map(|items| items.iter().any(|item| item.as_str() == Some(host_role)))
            .unwrap_or(false)
        {
            continue;
        }
        if let Some(bond) = network.get("bond") {
            let name = bond
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            if seen.contains(&name) {
                continue;
            }
            for interface in bond
                .get("interfaces")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
            {
                let text = interface.as_str().unwrap_or("").to_string();
                if seen.insert(text.clone()) {
                    ethernets.push(json!({"type": "phy", "id": text}));
                }
            }
            bonds.push(json!({"type": "bond", "id": name, "bondMode": bond.get("mode").cloned().unwrap_or(json!("active-backup")), "interfaces": bond.get("interfaces").cloned().unwrap_or(json!([]))}));
            seen.insert(name);
        } else if let Some(interface) = network.get("interface").and_then(Value::as_str) {
            if seen.insert(interface.to_string()) {
                ethernets.push(json!({"type": "phy", "id": interface}));
            }
        }
    }
    let mut vlans = Vec::new();
    for network in plan
        .get("networks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        let Some(vlan) = network.get("vlan").and_then(Value::as_i64) else {
            continue;
        };
        if !network
            .get("host_roles")
            .and_then(Value::as_array)
            .map(|items| items.iter().any(|item| item.as_str() == Some(host_role)))
            .unwrap_or(false)
        {
            continue;
        }
        let base = if let Some(name) = network
            .get("bond")
            .and_then(|bond| bond.get("name"))
            .and_then(Value::as_str)
        {
            name.to_string()
        } else if let Some(name) = network.get("interface").and_then(Value::as_str) {
            name.to_string()
        } else {
            vlan_base(plan, &network)?
        };
        let link = link_id(plan, &network)?;
        if seen.contains(&link) {
            continue;
        }
        if !seen.contains(&base) {
            let Some(definition) = base_link(plan, &base) else {
                return Err("INFRA_NETWORK_BASE_INTERFACE_UNDEFINED".into());
            };
            for interface in definition
                .get("interfaces")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
            {
                let text = interface.as_str().unwrap_or("").to_string();
                if seen.insert(text.clone()) {
                    ethernets.push(json!({"type": "phy", "id": text}));
                }
            }
            if definition
                .get("mode")
                .and_then(Value::as_str)
                .map(|mode| !mode.is_empty())
                .unwrap_or(false)
            {
                bonds.push(json!({"type": "bond", "id": base, "bondMode": definition.get("mode").cloned().unwrap_or(json!("")), "interfaces": definition.get("interfaces").cloned().unwrap_or(json!([]))}));
            }
            seen.insert(base.clone());
        }
        vlans.push(json!({"type": "vlan", "id": link, "vlanId": vlan, "links": [base]}));
        seen.insert(link);
    }
    Ok(json!({"ethernets": ethernets, "bonds": bonds, "vlans": vlans}))
}

fn role_network_data(plan: &Value, host_role: &str) -> Result<Value, String> {
    if !HOST_ROLES.contains(&host_role) {
        return Err("INFRA_NETWORK_HOST_ROLE_INVALID".into());
    }
    let links = role_links(plan, host_role)?;
    let declared_default = plan
        .get("networks")
        .and_then(Value::as_array)
        .map(|networks| {
            networks.iter().any(|network| {
                network
                    .get("default_route")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
            })
        })
        .unwrap_or(false);
    let mut ipv4 = Vec::new();
    let mut ipv4_dhcp = Vec::new();
    for network in plan
        .get("networks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        if !network
            .get("host_roles")
            .and_then(Value::as_array)
            .map(|items| items.iter().any(|item| item.as_str() == Some(host_role)))
            .unwrap_or(false)
        {
            continue;
        }
        if network.get("cidr").and_then(Value::as_str).is_none() {
            continue;
        }
        let link = link_id(plan, &network)?;
        let segment = dns_name(network.get("name").and_then(Value::as_str).unwrap_or(""))?;
        if network
            .get("dhcp")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            ipv4_dhcp.push(json!({"id": segment, "link": link}));
            continue;
        }
        let mut address = json!({"id": segment, "link": link, "ipAddressFromIPPool": segment});
        if let Some(gateway) = network.get("gateway_ipv4").and_then(Value::as_str) {
            let owns_default = network
                .get("default_route")
                .and_then(Value::as_bool)
                .unwrap_or(false)
                || (!declared_default
                    && network.get("role").and_then(Value::as_str) == Some("control"));
            if owns_default {
                address["routes"] = json!([{"destination": "0.0.0.0/0", "gateway": gateway}]);
            } else if network.get("role").and_then(Value::as_str) != Some("control") {
                address["routes"] = json!([{"destination": network.get("cidr").cloned().unwrap_or(json!("")), "gateway": gateway}]);
            }
        }
        ipv4.push(address);
    }
    if ipv4.is_empty() && ipv4_dhcp.is_empty() {
        return Err("INFRA_NETWORK_NO_ADDRESSED_SEGMENT".into());
    }
    let mut networks = Map::new();
    if !ipv4.is_empty() {
        networks.insert("ipv4".into(), Value::Array(ipv4));
    }
    if !ipv4_dhcp.is_empty() {
        networks.insert("ipv4DHCP".into(), Value::Array(ipv4_dhcp));
    }
    let mut data = json!({"links": links, "networks": networks});
    if !plan
        .get("dns_servers")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .is_empty()
    {
        data["services"] = json!({"dns": plan.get("dns_servers").cloned().unwrap_or(json!([]))});
    }
    Ok(data)
}

fn render(plan: &Value) -> Result<Value, String> {
    let mut role_data = Map::new();
    for role in HOST_ROLES {
        role_data.insert(role.into(), role_network_data(plan, role)?);
    }
    let mut pools = Vec::new();
    for network in plan
        .get("networks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        let entries = network
            .get("ip_pools")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if entries.is_empty() {
            continue;
        }
        let name = dns_name(network.get("name").and_then(Value::as_str).unwrap_or(""))?;
        let mut spec = json!({
            "clusterName": "network-preview",
            "namePrefix": format!("{name}-"),
            "pools": entries,
        });
        if let Some(gateway) = network.get("gateway_ipv4").and_then(Value::as_str) {
            spec["gateway"] = json!(gateway);
        }
        pools.push(json!({
            "apiVersion": "ipam.metal3.io/v1alpha1",
            "kind": "IPPool",
            "metadata": {"name": name, "namespace": "metal3"},
            "spec": spec,
        }));
    }
    Ok(json!({
        "networks": plan.get("networks").cloned().unwrap_or(json!([])),
        "dns_servers": plan.get("dns_servers").cloned().unwrap_or(json!([])),
        "role_network_data": role_data,
        "ip_pools": pools,
    }))
}

fn port_findings(plan: &Value, hosts: &[Value]) -> Value {
    let mut ports: Vec<String> = Vec::new();
    for network in plan
        .get("networks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        if let Some(bond) = network.get("bond") {
            for port in bond
                .get("interfaces")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
            {
                let text = port.as_str().unwrap_or("").to_string();
                if !text.is_empty() && !ports.contains(&text) {
                    ports.push(text);
                }
            }
        } else if let Some(text) = network.get("interface").and_then(Value::as_str) {
            if !ports.contains(&text.to_string()) {
                ports.push(text.to_string());
            }
        }
    }
    if ports.is_empty() {
        return json!({"ok": true, "ports": [], "warnings": [], "hosts": 0});
    }
    let mut collected = Vec::new();
    for host in hosts {
        let Some(name) = host.get("bmh_name").and_then(Value::as_str) else {
            continue;
        };
        let mut keys = BTreeSet::new();
        for nic in host
            .get("nics")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
        {
            for field in ["name", "pci_address", "mac"] {
                if let Some(value) = nic.get(field).and_then(Value::as_str) {
                    let lower = value.to_lowercase();
                    keys.insert(lower.clone());
                    keys.insert(if let Some(stripped) = lower.strip_prefix("pci@") {
                        stripped.into()
                    } else {
                        format!("pci@{lower}")
                    });
                }
            }
        }
        if let Some(boot) = host.get("boot_mac").and_then(Value::as_str) {
            keys.insert(boot.to_lowercase());
        }
        collected.push((name.to_string(), keys));
    }
    if collected.is_empty() {
        return json!({"ok": true, "ports": [], "warnings": [], "hosts": 0});
    }
    let mut findings = Vec::new();
    let mut warnings = Vec::new();
    for port in &ports {
        let lower = port.to_lowercase();
        let keys: BTreeSet<String> = [
            lower.clone(),
            lower
                .strip_prefix("pci@")
                .map(str::to_string)
                .unwrap_or_else(|| format!("pci@{lower}")),
        ]
        .into_iter()
        .collect();
        let matched: Vec<String> = collected
            .iter()
            .filter(|(_, host)| !keys.is_disjoint(host))
            .map(|(name, _)| name.clone())
            .collect();
        let missing: Vec<String> = collected
            .iter()
            .filter(|(name, _)| !matched.contains(name))
            .map(|(name, _)| name.clone())
            .collect();
        let ok = !matched.is_empty();
        let detail = if ok {
            format!(
                "{port} is bound by the profile and reported by {} host(s)",
                matched.len()
            )
        } else {
            format!("{port} is bound by the profile but no collected host reports it")
        };
        if !ok {
            warnings.push(format!("INFRA_NETWORK_PORT_NOT_COLLECTED: {detail}"));
        }
        findings.push(json!({"port": port, "matched": ok, "hosts": matched, "missing_on": missing, "code": if ok { "" } else { "INFRA_NETWORK_PORT_NOT_COLLECTED" }, "detail": detail}));
    }
    json!({"ok": findings.iter().all(|finding| finding.get("matched").and_then(Value::as_bool).unwrap_or(false)), "ports": findings, "warnings": warnings, "hosts": collected.len()})
}

async fn collected_hosts(pool: &sqlx::AnyPool, tenant_id: &str, project_id: &str) -> Vec<Value> {
    let rows = sqlx::query(
        "SELECT bmh_name, hardware_inventory, (SELECT name FROM core_agentnode node WHERE CAST(node.id AS TEXT) = CAST(machine.agent_node_id AS TEXT)) AS node_name FROM core_agentmachine machine WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ?",
    )
    .bind(tenant_id)
    .bind(project_id)
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    let mut hosts = Vec::new();
    for row in rows {
        let bmh: String = row.try_get("bmh_name").unwrap_or_default();
        let node: String = row.try_get("node_name").unwrap_or_default();
        let name = if bmh.is_empty() { node } else { bmh };
        if name.is_empty() {
            continue;
        }
        let inventory: Value = row
            .try_get::<Option<String>, _>("hardware_inventory")
            .ok()
            .flatten()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({}));
        hosts.push(json!({"bmh_name": name, "nics": inventory.get("nics").cloned().unwrap_or(json!([])), "boot_mac": inventory.get("boot_mac").cloned().unwrap_or(json!(""))}));
    }
    hosts
}

fn get_response(profile: Value, generated: Option<Value>, port_inventory: Option<Value>) -> Value {
    if port_inventory.is_some() {
        json!({"profile": profile, "generated": generated, "port_inventory": port_inventory})
    } else {
        json!({"profile": profile, "generated": generated})
    }
}

fn parse_request_profile(body: &Bytes) -> Result<Value, Response> {
    let payload: Value =
        serde_json::from_slice(body).map_err(|_| fail("profile must be a JSON object."))?;
    let profile = payload.get("profile").cloned().unwrap_or(payload);
    if !profile.is_object() {
        return Err(fail("profile must be a JSON object."));
    }
    Ok(profile)
}

async fn scope(
    state: &AppState,
    headers: &HeaderMap,
    tenant_id: &str,
    project_id: &str,
) -> Result<(i64, String), Response> {
    let user = crate::billing_api::require_user(state, headers).await?;
    let pool = state.identity_pool.as_ref().ok_or_else(unavailable)?;
    if !project_access(pool, tenant_id, project_id, user.id)
        .await
        .map_err(db_error)?
    {
        return Err(not_found());
    }
    let role = crate::tenancy::tenant_role(pool, tenant_id, user.id)
        .await
        .map_err(db_error)?
        .unwrap_or_default();
    Ok((user.id, role))
}

async fn editable(
    state: &AppState,
    headers: &HeaderMap,
    tenant_id: &str,
    project_id: &str,
) -> Result<sqlx::AnyPool, Response> {
    let (user_id, role) = scope(state, headers, tenant_id, project_id).await?;
    let pool = state.identity_pool.as_ref().ok_or_else(unavailable)?;
    if !crate::workflows::editor(&role, pool, project_id, user_id).await {
        return Err((
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Project editor permission required."})),
        )
            .into_response());
    }
    Ok(pool.clone())
}

/// `GET|PUT|DELETE /api/v1/tenants/{t}/projects/{p}/infrastructure-networks/`
pub async fn profile(
    State(state): State<AppState>,
    method: axum::http::Method,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    if method == axum::http::Method::GET {
        let (_, _) = match scope(&state, &headers, &tenant_id, &project_id).await {
            Ok(value) => value,
            Err(response) => return response,
        };
        let pool = match state.identity_pool.as_ref() {
            Some(pool) => pool,
            None => return unavailable(),
        };
        let stored: Option<String> = sqlx::query_scalar(
            "SELECT profile FROM core_infrastructurenetworkprofile WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? LIMIT 1",
        ).bind(&tenant_id).bind(&project_id).fetch_optional(pool).await.unwrap_or(None);
        let profile: Value = stored
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({}));
        if profile
            .get("networks")
            .and_then(Value::as_array)
            .map(Vec::is_empty)
            .unwrap_or(true)
        {
            return Json(get_response(profile, None, None)).into_response();
        }
        let planned = match plan(&profile) {
            Ok(value) => value,
            Err(code) => {
                return (
                    StatusCode::CONFLICT,
                    Json(json!({"profile": profile, "generated": null, "detail": code})),
                )
                    .into_response()
            }
        };
        let generated = match render(&planned) {
            Ok(value) => value,
            Err(code) => {
                return (
                    StatusCode::CONFLICT,
                    Json(json!({"profile": profile, "generated": null, "detail": code})),
                )
                    .into_response()
            }
        };
        let hosts = collected_hosts(pool, &tenant_id, &project_id).await;
        let findings = port_findings(&planned, &hosts);
        return Json(get_response(profile, Some(generated), Some(findings))).into_response();
    }
    let pool = match editable(&state, &headers, &tenant_id, &project_id).await {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    if method == axum::http::Method::DELETE {
        let _ = sqlx::query("DELETE FROM core_infrastructurenetworkprofile WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ?")
            .bind(&tenant_id).bind(&project_id).execute(&pool).await;
        let _ = crate::audit::record_event(
            &pool,
            state.config.database_url.as_deref().unwrap_or_default(),
            crate::audit::AuditEvent {
                tenant_id: Some(&tenant_id),
                project_id: Some(&project_id),
                event_type: "infrastructure_network_profile.deleted",
                resource_type: "infrastructure_network_profile",
                resource_id: "",
                outcome: "success",
                actor_id: None,
                request_id: "",
                remote_addr: None,
                user_agent: "",
                metadata: json!({}),
            },
        )
        .await;
        return StatusCode::NO_CONTENT.into_response();
    }
    let profile = match parse_request_profile(&body) {
        Ok(profile) => profile,
        Err(response) => return response,
    };
    let planned = match plan(&profile) {
        Ok(plan) => plan,
        Err(code) => return fail(&code),
    };
    let generated = match render(&planned) {
        Ok(value) => value,
        Err(code) => return fail(&code),
    };
    let stored = json!({"networks": planned.get("networks").cloned().unwrap_or(json!([])), "dns_servers": planned.get("dns_servers").cloned().unwrap_or(json!([]))});
    let existing: Option<String> = sqlx::query_scalar("SELECT CAST(id AS TEXT) FROM core_infrastructurenetworkprofile WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? LIMIT 1").bind(&tenant_id).bind(&project_id).fetch_optional(&pool).await.unwrap_or(None);
    if let Some(id) = existing {
        let _ = sqlx::query("UPDATE core_infrastructurenetworkprofile SET profile = ?, updated_at = CURRENT_TIMESTAMP WHERE CAST(id AS TEXT) = ?")
            .bind(stored.to_string()).bind(&id).execute(&pool).await;
    } else {
        let _ = sqlx::query("INSERT INTO core_infrastructurenetworkprofile (id, tenant_id, project_id, profile, created_at, updated_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
            .bind(uuid::Uuid::new_v4().to_string()).bind(&tenant_id).bind(&project_id).bind(stored.to_string()).execute(&pool).await;
    }
    let hosts = collected_hosts(&pool, &tenant_id, &project_id).await;
    let findings = port_findings(&planned, &hosts);
    let _ = crate::audit::record_event(&pool, state.config.database_url.as_deref().unwrap_or_default(), crate::audit::AuditEvent {
        tenant_id: Some(&tenant_id), project_id: Some(&project_id), event_type: "infrastructure_network_profile.updated",
        resource_type: "infrastructure_network_profile", resource_id: "", outcome: "success", actor_id: None,
        request_id: "", remote_addr: None, user_agent: "", metadata: json!({"network_count": planned.get("networks").and_then(Value::as_array).map(Vec::len).unwrap_or(0)}),
    }).await;
    Json(json!({
        "profile": stored,
        "generated": generated,
        "port_inventory": findings,
        "warnings": findings.get("warnings").cloned().unwrap_or(json!([])),
    }))
    .into_response()
}
