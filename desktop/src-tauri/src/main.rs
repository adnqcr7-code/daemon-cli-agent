#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;
use std::{
    fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    thread,
};
use tauri::{AppHandle, Emitter};

static RUN_COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Serialize)]
struct DaemonStatus {
    installed: bool,
    version: String,
    detail: String,
}

#[derive(Serialize)]
struct WorkspaceEntry {
    name: String,
    path: String,
    kind: String,
    size: u64,
}

#[derive(Serialize, Clone)]
struct TaskOutput {
    run_id: String,
    stream: String,
    line: String,
}

#[derive(Serialize, Clone)]
struct TaskFinished {
    run_id: String,
    success: bool,
    code: Option<i32>,
}

fn daemon_cmd() -> Command {
    let executable = if cfg!(target_os = "windows") { "daemon.exe" } else { "daemon" };
    if let Some(paths) = std::env::var_os("PATH") {
        for directory in std::env::split_paths(&paths) {
            let candidate = directory.join(executable);
            if candidate.is_file() { return Command::new(candidate); }
        }
    }
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from);
    if let Some(home) = home {
        let mut candidates = vec![
            home.join(".local/bin").join(executable),
            home.join(".local/pipx/venvs/daemon-agent/bin").join(executable),
            home.join(".local/share/pipx/venvs/daemon-agent/bin").join(executable),
            home.join("pipx/venvs/daemon-agent/Scripts").join(executable),
            home.join(".local/pipx/venvs/daemon-agent/Scripts").join(executable),
        ];
        if let Some(appdata) = std::env::var_os("APPDATA") {
            candidates.push(PathBuf::from(appdata).join("Python/Scripts").join(executable));
            candidates.push(PathBuf::from(appdata).join("Python/Python312/Scripts").join(executable));
        }
        if let Some(found) = candidates.into_iter().find(|path| path.is_file()) {
            return Command::new(found);
        }
    }
    Command::new("daemon")
}

fn clean_workspace(raw: &str) -> Result<PathBuf, String> {
    let path = Path::new(raw);
    let canonical = fs::canonicalize(path).map_err(|e| format!("Cannot open workspace: {e}"))?;
    if !canonical.is_dir() {
        return Err("Workspace path is not a directory".into());
    }
    Ok(canonical)
}

#[tauri::command]
fn daemon_status() -> DaemonStatus {
    match daemon_cmd().args(["version", "--verbose"]).output() {
        Ok(output) if output.status.success() => {
            let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
            let version = text.lines().next().unwrap_or("Daemon is installed").to_string();
            DaemonStatus { installed: true, version, detail: text }
        }
        Ok(output) => {
            let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
            DaemonStatus { installed: false, version: "Not available".into(), detail }
        }
        Err(error) => DaemonStatus {
            installed: false,
            version: "Not found".into(),
            detail: format!("The `daemon` executable is not on PATH: {error}"),
        },
    }
}

#[tauri::command]
fn list_workspace(workspace: String, subpath: Option<String>) -> Result<Vec<WorkspaceEntry>, String> {
    let root = clean_workspace(&workspace)?;
    let target = match subpath {
        Some(path) if !path.trim().is_empty() => {
            let canonical = fs::canonicalize(path).map_err(|e| format!("Cannot open folder: {e}"))?;
            if !canonical.starts_with(&root) || !canonical.is_dir() {
                return Err("Folder is outside the selected workspace".into());
            }
            canonical
        }
        _ => root.clone(),
    };
    let mut entries = Vec::new();
    let read = fs::read_dir(&target).map_err(|e| format!("Cannot read workspace: {e}"))?;
    for item in read.flatten() {
        let name = item.file_name().to_string_lossy().to_string();
        if matches!(name.as_str(), ".git" | ".daemon" | ".venv" | ".cache" | "node_modules" | "target" | "dist" | "__pycache__" | ".pytest_cache" | ".mypy_cache" | ".ruff_cache") {
            continue;
        }
        let metadata = match item.metadata() { Ok(value) => value, Err(_) => continue };
        let kind = if metadata.is_dir() { "directory" } else { "file" };
        entries.push(WorkspaceEntry {
            name,
            path: item.path().to_string_lossy().to_string(),
            kind: kind.into(),
            size: metadata.len(),
        });
    }
    entries.sort_by(|a, b| {
        let ak = if a.kind == "directory" { 0 } else { 1 };
        let bk = if b.kind == "directory" { 0 } else { 1 };
        ak.cmp(&bk).then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
    });
    entries.truncate(160);
    Ok(entries)
}

#[tauri::command]
fn read_workspace_file(workspace: String, file_path: String) -> Result<String, String> {
    let root = clean_workspace(&workspace)?;
    let target = fs::canonicalize(&file_path).map_err(|e| format!("Cannot open file: {e}"))?;
    if !target.starts_with(&root) {
        return Err("File is outside the selected workspace".into());
    }
    let metadata = fs::metadata(&target).map_err(|e| e.to_string())?;
    if !metadata.is_file() || metadata.len() > 180_000 {
        return Err("Preview is limited to files under 180 KB".into());
    }
    let bytes = fs::read(&target).map_err(|e| e.to_string())?;
    if bytes.iter().take(4096).any(|b| *b == 0) {
        return Err("Binary files cannot be previewed".into());
    }
    Ok(String::from_utf8_lossy(&bytes).to_string())
}

#[tauri::command]
fn git_status(workspace: String) -> Result<String, String> {
    let root = clean_workspace(&workspace)?;
    let output = Command::new("git").arg("-C").arg(root).args(["status", "--short", "--branch"]).output()
        .map_err(|e| format!("Could not run git: {e}"))?;
    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    } else {
        Ok("Not a Git repository".into())
    }
}

#[tauri::command]
fn run_utility(workspace: String, action: String) -> Result<String, String> {
    let root = clean_workspace(&workspace)?;
    let mut command = daemon_cmd();
    match action.as_str() {
        "doctor" => { command.args(["doctor", "--workspace"]).arg(&root); }
        "models" => { command.args(["models", "list", "--workspace"]).arg(&root); }
        "auth" => { command.args(["auth", "list"]); }
        "sessions" => { command.args(["sessions", "list"]); }
        "agents" => { command.args(["agents"]); }
        "tools" => { command.args(["tools"]); }
        "mcp" => { command.args(["mcp", "list"]); }
        "version" => { command.args(["version", "--verbose"]); }
        _ => return Err("Unsupported Daemon action".into()),
    }
    let output = command.output()
        .map_err(|e| format!("Could not launch Daemon: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let result = if stdout.trim().is_empty() { stderr.to_string() } else { stdout.to_string() };
    Ok(result.trim().to_string())
}

#[tauri::command]
fn execute_task(app: AppHandle, workspace: String, prompt: String, mode: String, model: Option<String>) -> Result<String, String> {
    if prompt.trim().is_empty() { return Err("Enter a task first".into()); }
    let root = clean_workspace(&workspace)?;
    let run_id = format!("run-{}", RUN_COUNTER.fetch_add(1, Ordering::Relaxed));
    let id = run_id.clone();
    thread::spawn(move || {
        let mut command = daemon_cmd();
        match mode.as_str() {
            "plan" => {
                command.args(["run", "--plan", "--workspace"]).arg(&root);
                if let Some(model_id) = model.as_deref().filter(|value| !value.trim().is_empty()) {
                    command.args(["--model", model_id]);
                }
                command.arg("--").arg(prompt.trim());
            }
            "research" => {
                command.args(["research", "--workspace"]).arg(&root).arg("--").arg(prompt.trim());
            }
            _ => {
                command.args(["run", "--workspace"]).arg(&root);
                if let Some(model_id) = model.as_deref().filter(|value| !value.trim().is_empty()) {
                    command.args(["--model", model_id]);
                }
                command.arg("--").arg(prompt.trim());
            }
        }
        let spawn = command.stdout(Stdio::piped()).stderr(Stdio::piped()).spawn();
        let mut child = match spawn {
            Ok(child) => child,
            Err(error) => {
                let _ = app.emit("daemon-output", TaskOutput { run_id: id.clone(), stream: "stderr".into(), line: format!("Could not start Daemon: {error}") });
                let _ = app.emit("daemon-complete", TaskFinished { run_id: id, success: false, code: None });
                return;
            }
        };
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();
        let app_out = app.clone();
        let id_out = id.clone();
        let out_thread = thread::spawn(move || {
            if let Some(pipe) = stdout {
                for line in BufReader::new(pipe).lines().flatten() {
                    let _ = app_out.emit("daemon-output", TaskOutput { run_id: id_out.clone(), stream: "stdout".into(), line });
                }
            }
        });
        let app_err = app.clone();
        let id_err = id.clone();
        let err_thread = thread::spawn(move || {
            if let Some(pipe) = stderr {
                for line in BufReader::new(pipe).lines().flatten() {
                    let _ = app_err.emit("daemon-output", TaskOutput { run_id: id_err.clone(), stream: "stderr".into(), line });
                }
            }
        });
        let status = child.wait();
        let _ = out_thread.join();
        let _ = err_thread.join();
        let finished = match status {
            Ok(status) => TaskFinished { run_id: id, success: status.success(), code: status.code() },
            Err(_) => TaskFinished { run_id: id, success: false, code: None },
        };
        let _ = app.emit("daemon-complete", finished);
    });
    Ok(run_id)
}

#[tauri::command]
fn generate_agent_prompt(workspace: String, name: String, specialty: String, brief: String) -> Result<String, String> {
    let root = clean_workspace(&workspace)?;
    if name.trim().is_empty() || brief.trim().is_empty() {
        return Err("Agent name and purpose are required".into());
    }
    let request = format!(
        "Create a reusable system instruction prompt for a specialist AI coding agent.\nAgent name: {}\nSpecialty: {}\nUser brief: {}\n\nReturn ONLY the final system instructions, no preamble, no markdown fences. Make the agent's role, workflow, boundaries, tool-use expectations, verification standards, and output format specific and practical. Do not ask questions. Do not claim access to tools it may not have. Preserve the host runtime's safety and permission rules.",
        name.trim(), specialty.trim(), brief.trim()
    );
    let output = daemon_cmd()
        .args(["research", "--workspace"])
        .arg(&root)
        .arg("--")
        .arg(request)
        .output()
        .map_err(|e| format!("Could not launch Daemon research: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    if !output.status.success() {
        return Err(if stderr.is_empty() { stdout } else { stderr });
    }
    if stdout.is_empty() { return Err("The configured model returned no agent instructions".into()); }
    Ok(stdout)
}

#[tauri::command]
fn reveal_workspace(path: String) -> Result<(), String> {
    let target = clean_workspace(&path)?;
    #[cfg(target_os = "linux")]
    let status = Command::new("xdg-open").arg(target).status();
    #[cfg(target_os = "macos")]
    let status = Command::new("open").arg(target).status();
    #[cfg(target_os = "windows")]
    let status = Command::new("explorer").arg(target).status();
    status.map(|_| ()).map_err(|e| format!("Could not open folder: {e}"))
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            daemon_status, list_workspace, read_workspace_file, git_status,
            run_utility, execute_task, generate_agent_prompt, reveal_workspace
        ])
        .run(tauri::generate_context!())
        .expect("error while running Daemon desktop");
}
