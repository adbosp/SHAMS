//! SHAMS desktop shell (ADR-119, ADR-122).
//!
//! The shell owns one thing: the lifecycle of the SHAMS Core Service (the Python backend).
//! * On start it attaches to a backend that already answers on the configured port, or starts
//!   `python -m apps.cli.main desktop-backend` from the configured SHAMS folder on a free port with a
//!   random token, and waits for `/api/health`.
//! * The web view asks `backend_info` for the URL + token; everything else goes over the local HTTP API.
//! * On exit it asks the backend to stop (`/api/desktop/shutdown`) and, only if that fails, kills the
//!   child process it started itself. No other process is touched.
//!
//! Configuration lives in `<app config dir>/config.json` (Windows: `%APPDATA%\com.shams.desktop\config.json`),
//! written with defaults on first start. Environment variables `SHAMS_DIR`, `SHAMS_PYTHON`,
//! `SHAMS_WORKSPACE` and `SHAMS_PORT` override it (handy for development).

use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{Emitter, Manager, RunEvent};

#[derive(Clone, Serialize, Deserialize, Debug)]
struct ShellConfig {
    /// SHAMS repository / install folder (contains `apps/` and `shams/`).
    shams_dir: String,
    /// Python interpreter with SHAMS installed (usually the repo's .venv).
    python: String,
    /// SHAMS workspace folder (autonomous projects, desktop history).
    workspace: String,
    /// Port tried first; a running backend there is reused when `attach_existing` is true.
    port: u16,
    attach_existing: bool,
    /// Seconds to wait for /api/health after starting the backend.
    start_timeout_s: u64,
}

#[derive(Clone, Serialize, Default)]
struct BackendInfo {
    url: String,
    token: Option<String>,
    managed: bool,
    log: Option<String>,
}

struct Backend {
    child: Option<Child>,
    info: BackendInfo,
    log: Arc<Mutex<VecDeque<String>>>,
    error: Option<String>,
}

type Shared = Arc<Mutex<Backend>>;

fn exists(p: &Path) -> bool {
    p.join("apps").join("cli").join("main.py").exists()
}

fn default_config() -> ShellConfig {
    let home = std::env::var("USERPROFILE").or_else(|_| std::env::var("HOME")).unwrap_or_else(|_| ".".into());
    let mut candidates: Vec<PathBuf> = vec![];
    if let Ok(exe) = std::env::current_exe() {
        for a in exe.ancestors().skip(1).take(6) {
            candidates.push(a.to_path_buf());
        }
    }
    candidates.push(PathBuf::from(r"C:\AI\SHAMS"));
    candidates.push(PathBuf::from(&home).join("SHAMS-src"));
    let shams_dir = candidates.into_iter().find(|p| exists(p)).unwrap_or_else(|| PathBuf::from(r"C:\AI\SHAMS"));
    let venv = if cfg!(windows) { shams_dir.join(".venv").join("Scripts").join("python.exe") } else { shams_dir.join(".venv").join("bin").join("python") };
    let python = if venv.exists() { venv.to_string_lossy().to_string() } else if cfg!(windows) { "python".into() } else { "python3".into() };
    let ws_default = if PathBuf::from(r"C:\AI\SHAMS_Projects").exists() { PathBuf::from(r"C:\AI\SHAMS_Projects") } else { PathBuf::from(&home).join("SHAMS") };
    ShellConfig {
        shams_dir: shams_dir.to_string_lossy().to_string(),
        python,
        workspace: ws_default.to_string_lossy().to_string(),
        port: 8765,
        attach_existing: true,
        start_timeout_s: 90,
    }
}

fn config_path(app: &tauri::AppHandle) -> PathBuf {
    app.path().app_config_dir().unwrap_or_else(|_| PathBuf::from(".")).join("config.json")
}

fn load_config(app: &tauri::AppHandle) -> ShellConfig {
    let path = config_path(app);
    let mut cfg = fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str::<ShellConfig>(&s).ok())
        .unwrap_or_else(|| {
            let c = default_config();
            if let Some(dir) = path.parent() {
                let _ = fs::create_dir_all(dir);
            }
            let _ = fs::write(&path, serde_json::to_string_pretty(&c).unwrap_or_default());
            c
        });
    if let Ok(v) = std::env::var("SHAMS_DIR") { cfg.shams_dir = v; }
    if let Ok(v) = std::env::var("SHAMS_PYTHON") { cfg.python = v; }
    if let Ok(v) = std::env::var("SHAMS_WORKSPACE") { cfg.workspace = v; }
    if let Ok(v) = std::env::var("SHAMS_PORT") { if let Ok(p) = v.parse() { cfg.port = p; } }
    cfg
}

/// Minimal HTTP/1.1 request to the local backend (no extra crates). Returns (status, body).
fn http(method: &str, port: u16, path: &str, token: Option<&str>, timeout: Duration) -> Option<(u16, String)> {
    let mut s = TcpStream::connect_timeout(&format!("127.0.0.1:{port}").parse().ok()?, timeout).ok()?;
    s.set_read_timeout(Some(timeout)).ok()?;
    s.set_write_timeout(Some(timeout)).ok()?;
    let tok = token.map(|t| format!("X-Shams-Token: {t}\r\n")).unwrap_or_default();
    let body = if method == "POST" { "{}" } else { "" };
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{tok}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    s.write_all(req.as_bytes()).ok()?;
    let mut buf = String::new();
    s.read_to_string(&mut buf).ok()?;
    let status = buf.split_whitespace().nth(1)?.parse().ok()?;
    let body = buf.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
    Some((status, body))
}

fn healthy(port: u16) -> bool {
    matches!(http("GET", port, "/api/health", None, Duration::from_millis(800)), Some((200, b)) if b.contains("\"SHAMS\""))
}

fn free_port(preferred: u16) -> u16 {
    if TcpListener::bind(("127.0.0.1", preferred)).is_ok() {
        return preferred;
    }
    TcpListener::bind(("127.0.0.1", 0)).ok().and_then(|l| l.local_addr().ok()).map(|a| a.port()).unwrap_or(preferred)
}

fn push_log(log: &Arc<Mutex<VecDeque<String>>>, line: String) {
    if let Ok(mut l) = log.lock() {
        l.push_back(line);
        while l.len() > 400 {
            l.pop_front();
        }
    }
}

fn start_backend(app: &tauri::AppHandle, shared: &Shared) {
    let cfg = load_config(app);
    let log = shared.lock().unwrap().log.clone();
    // 1) a backend already running (started by hand or by another client): attach to it
    if cfg.attach_existing && healthy(cfg.port) {
        let info = BackendInfo { url: format!("http://127.0.0.1:{}", cfg.port), token: None, managed: false, log: None };
        push_log(&log, format!("attached to running backend on port {}", cfg.port));
        {
            let mut b = shared.lock().unwrap();
            b.info = info.clone();
            b.error = None;
        }
        let _ = app.emit("backend://ready", info);
        return;
    }
    // 2) start our own
    let port = free_port(cfg.port);
    let token = uuid::Uuid::new_v4().simple().to_string();
    let mut cmd = Command::new(&cfg.python);
    cmd.args(["-m", "apps.cli.main", "desktop-backend", &cfg.workspace, "--port", &port.to_string()])
        .current_dir(&cfg.shams_dir)
        .env("SHAMS_DESKTOP_TOKEN", &token)
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    push_log(&log, format!("starting: {} -m apps.cli.main desktop-backend {} --port {port} (cwd {})", cfg.python, cfg.workspace, cfg.shams_dir));
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let msg = format!("cannot start Python ({}): {e}. Set \"python\" and \"shams_dir\" in {}", cfg.python, config_path(app).display());
            push_log(&log, msg.clone());
            shared.lock().unwrap().error = Some(msg);
            let _ = app.emit("backend://error", ());
            return;
        }
    };
    let log_file = app.path().app_log_dir().ok().map(|d| {
        let _ = fs::create_dir_all(&d);
        d.join("backend.log")
    });
    for pipe in [child.stdout.take().map(|p| Box::new(p) as Box<dyn Read + Send>), child.stderr.take().map(|p| Box::new(p) as Box<dyn Read + Send>)]
        .into_iter()
        .flatten()
    {
        let log = log.clone();
        let file = log_file.clone();
        std::thread::spawn(move || {
            let mut f = file.and_then(|p| fs::OpenOptions::new().create(true).append(true).open(p).ok());
            for line in BufReader::new(pipe).lines().map_while(Result::ok) {
                if let Some(f) = f.as_mut() {
                    let _ = writeln!(f, "{line}");
                }
                push_log(&log, line);
            }
        });
    }
    {
        let mut b = shared.lock().unwrap();
        b.child = Some(child);
        b.error = None;
    }
    let deadline = Instant::now() + Duration::from_secs(cfg.start_timeout_s);
    while Instant::now() < deadline {
        if healthy(port) {
            let info = BackendInfo { url: format!("http://127.0.0.1:{port}"), token: Some(token.clone()), managed: true, log: None };
            shared.lock().unwrap().info = info.clone();
            let _ = app.emit("backend://ready", info);
            return;
        }
        // the process died while starting: report its output
        let exited = {
            let mut b = shared.lock().unwrap();
            b.child.as_mut().and_then(|c| c.try_wait().ok().flatten())
        };
        if let Some(code) = exited {
            let msg = format!("backend exited while starting ({code}); see the log below");
            push_log(&log, msg.clone());
            let mut b = shared.lock().unwrap();
            b.child = None;
            b.error = Some(msg);
            let _ = app.emit("backend://error", ());
            return;
        }
        std::thread::sleep(Duration::from_millis(400));
    }
    let msg = format!("backend did not answer /api/health within {}s", cfg.start_timeout_s);
    push_log(&log, msg.clone());
    shared.lock().unwrap().error = Some(msg);
    let _ = app.emit("backend://error", ());
}

fn stop_backend(shared: &Shared) {
    let (child, info) = {
        let mut b = shared.lock().unwrap();
        (b.child.take(), b.info.clone())
    };
    let Some(mut child) = child else { return };
    // graceful first: the backend finishes its current step, closes SQLite and exits
    if let Some(port) = info.url.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()) {
        let _ = http("POST", port, "/api/desktop/shutdown", info.token.as_deref(), Duration::from_secs(2));
    }
    let deadline = Instant::now() + Duration::from_secs(8);
    while Instant::now() < deadline {
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    // only the process this shell started
    let _ = child.kill();
    let _ = child.wait();
}

#[tauri::command]
fn backend_info(state: tauri::State<'_, Shared>) -> Result<BackendInfo, String> {
    let b = state.lock().map_err(|e| e.to_string())?;
    if let Some(e) = &b.error {
        return Err(e.clone());
    }
    Ok(b.info.clone())
}

#[tauri::command]
fn backend_log(state: tauri::State<'_, Shared>) -> String {
    let b = state.lock().unwrap();
    let l = b.log.lock().unwrap();
    l.iter().cloned().collect::<Vec<_>>().join("\n")
}

#[tauri::command]
fn shell_config(app: tauri::AppHandle) -> serde_json::Value {
    let cfg = load_config(&app);
    let mut v = serde_json::to_value(cfg).unwrap_or_default();
    v["config_path"] = serde_json::Value::String(config_path(&app).to_string_lossy().to_string());
    v
}

#[tauri::command]
fn restart_backend(app: tauri::AppHandle, state: tauri::State<'_, Shared>) {
    let shared = state.inner().clone();
    stop_backend(&shared);
    {
        let mut b = shared.lock().unwrap();
        b.info = BackendInfo::default();
        b.error = None;
    }
    std::thread::spawn(move || start_backend(&app, &shared));
}

pub fn run() {
    let shared: Shared = Arc::new(Mutex::new(Backend { child: None, info: BackendInfo::default(), log: Arc::new(Mutex::new(VecDeque::new())), error: None }));
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(shared.clone())
        .invoke_handler(tauri::generate_handler![backend_info, backend_log, shell_config, restart_backend])
        .setup(|app| {
            let handle = app.handle().clone();
            let shared = app.state::<Shared>().inner().clone();
            std::thread::spawn(move || start_backend(&handle, &shared));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building SHAMS desktop");
    app.run(move |_handle, event| {
        if let RunEvent::Exit = event {
            stop_backend(&shared);
        }
    });
}
