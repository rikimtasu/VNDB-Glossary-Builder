//! VNDB Glossary Builder backend.
//!
//! The frontend (TypeScript) owns all glossary logic. Rust only does what a
//! webview cannot: talk to api.vndb.org without CORS trouble (with retries
//! and back-off, mirroring the old Python client) and touch the filesystem.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use reqwest::StatusCode;
use serde::Serialize;
use std::time::Duration;

const USER_AGENT: &str = "vndb-glossary-builder/1.0 (tauri)";
const MAX_RETRIES: u32 = 3;

#[tauri::command]
fn ping() -> &'static str {
    "pong"
}

#[derive(Debug)]
struct VndbRequest {
    endpoint: String,
    path: String,
    payload: serde_json::Value,
    token: String,
}

fn is_retryable(status: StatusCode) -> bool {
    matches!(
        status,
        StatusCode::TOO_MANY_REQUESTS
            | StatusCode::INTERNAL_SERVER_ERROR
            | StatusCode::BAD_GATEWAY
            | StatusCode::SERVICE_UNAVAILABLE
            | StatusCode::GATEWAY_TIMEOUT
    )
}

/// POST one query to the Kana API, retrying 429/5xx with back-off.
///
/// Takes flat arguments so the frontend can call
/// `invoke("vndb_request", { endpoint, path, payload, token })` directly.
#[tauri::command]
async fn vndb_request(
    endpoint: String,
    path: String,
    payload: serde_json::Value,
    token: String,
) -> Result<serde_json::Value, String> {
    vndb_request_inner(VndbRequest { endpoint, path, payload, token }).await
}

async fn vndb_request_inner(req: VndbRequest) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| format!("Could not build HTTP client: {e}"))?;

    let url = format!("{}/{}", req.endpoint.trim_end_matches('/'), req.path.trim_start_matches('/'));
    let mut delay = Duration::from_secs(2);

    for attempt in 0..=MAX_RETRIES {
        let mut builder = client
            .post(&url)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .json(&req.payload);
        if !req.token.trim().is_empty() {
            builder = builder.header("Authorization", format!("Token {}", req.token.trim()));
        }

        let response = match builder.send().await {
            Ok(r) => r,
            Err(e) if attempt < MAX_RETRIES => {
                tokio::time::sleep(delay).await;
                delay *= 2;
                let _ = e;
                continue;
            }
            Err(e) => return Err(format!("Could not reach {url}: {e}")),
        };

        let status = response.status();
        if is_retryable(status) && attempt < MAX_RETRIES {
            let wait = response
                .headers()
                .get("retry-after")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.trim().parse::<f64>().ok())
                .map(Duration::from_secs_f64)
                .unwrap_or(delay);
            tokio::time::sleep(wait).await;
            delay *= 2;
            continue;
        }
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            let body = body.trim();
            return Err(if body.is_empty() {
                format!("HTTP {}: {}", status.as_u16(), status.canonical_reason().unwrap_or("error"))
            } else {
                format!("HTTP {}: {}", status.as_u16(), body.lines().next().unwrap_or(body))
            });
        }
        let text = response.text().await.map_err(|e| format!("Could not read response: {e}"))?;
        if text.trim().is_empty() {
            return Ok(serde_json::Value::Object(Default::default()));
        }
        return serde_json::from_str(&text).map_err(|e| format!("Invalid JSON from API: {e}"));
    }
    Err(format!("Could not reach {url}: retries exhausted"))
}

// ---------------------------------------------------------------------------
// filesystem (kept in Rust so no FS-scope allowlisting is needed)
// ---------------------------------------------------------------------------

#[tauri::command]
fn read_text_file(path: String) -> Result<String, String> {
    // Accept the UTF-8 BOM the same way the Python version did.
    let bytes = std::fs::read(&path).map_err(|e| format!("Could not read {path}: {e}"))?;
    let text = String::from_utf8_lossy(&bytes).into_owned();
    Ok(text.strip_prefix('\u{feff}').unwrap_or(&text).to_string())
}

#[tauri::command]
fn write_text_file(path: String, contents: String) -> Result<(), String> {
    if let Some(parent) = std::path::Path::new(&path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(|e| format!("Could not create folder: {e}"))?;
        }
    }
    std::fs::write(&path, contents.as_bytes()).map_err(|e| format!("Could not write {path}: {e}"))
}

#[tauri::command]
fn read_binary_file(path: String) -> Result<Vec<u8>, String> {
    std::fs::read(&path).map_err(|e| format!("Could not read {path}: {e}"))
}

#[tauri::command]
fn write_binary_file(path: String, data: Vec<u8>) -> Result<(), String> {
    if let Some(parent) = std::path::Path::new(&path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(|e| format!("Could not create folder: {e}"))?;
        }
    }
    std::fs::write(&path, data).map_err(|e| format!("Could not write {path}: {e}"))
}

#[derive(Debug, Serialize)]
struct DirEntry {
    name: String,
    is_dir: bool,
}

#[tauri::command]
fn list_dir(path: String) -> Result<Vec<DirEntry>, String> {
    let entries = std::fs::read_dir(&path).map_err(|e| format!("Could not read {path}: {e}"))?;
    let mut out = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| format!("Could not list {path}: {e}"))?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let is_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
        out.push(DirEntry { name, is_dir });
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(out)
}

#[tauri::command]
fn create_dir_all(path: String) -> Result<(), String> {
    std::fs::create_dir_all(&path).map_err(|e| format!("Could not create {path}: {e}"))
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            ping,
            vndb_request,
            read_text_file,
            write_text_file,
            read_binary_file,
            write_binary_file,
            list_dir,
            create_dir_all
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Hits the live API: the exact query the GUI issues for "load by vndbid".
    #[tokio::test]
    async fn live_vn_lookup_by_id() {
        let req = VndbRequest {
            endpoint: "https://api.vndb.org/kana".to_string(),
            path: "vn".to_string(),
            payload: serde_json::json!({
                "filters": ["id", "=", "v17"],
                "fields": "id,title",
                "results": 1,
                "page": 1,
            }),
            token: String::new(),
        };
        let value = vndb_request_inner(req).await.expect("API should be reachable");
        assert_eq!(value["results"][0]["id"], "v17");
    }

    /// Hits the live API: first page of a VN's character cast.
    #[tokio::test]
    async fn live_character_page() {
        let req = VndbRequest {
            endpoint: "https://api.vndb.org/kana".to_string(),
            path: "character".to_string(),
            payload: serde_json::json!({
                "filters": ["and", ["vn", "=", ["id", "=", "v17"]]],
                "fields": "id,name,original",
                "results": 100,
                "page": 1,
                "sort": "id",
            }),
            token: String::new(),
        };
        let value = vndb_request_inner(req).await.expect("API should be reachable");
        let results = value["results"].as_array().expect("results array");
        assert!(!results.is_empty(), "v17 should have characters");
        assert!(results.iter().any(|c| c["original"].as_str().is_some_and(|s| !s.is_empty())));
    }
}
