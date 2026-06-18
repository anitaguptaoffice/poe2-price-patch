use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::Command;
use tauri::Manager;

#[derive(Serialize)]
struct RunResult {
    ok: bool,
    status: i32,
    stdout: String,
    stderr: String,
    output_dir: String,
}

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has parent")
        .to_path_buf()
}

fn core_binary_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "poe2-price-patcher.exe"
    } else {
        "poe2-price-patcher"
    }
}

fn candidate_core_binary(app: &tauri::AppHandle) -> Option<PathBuf> {
    let name = core_binary_name();
    let packaged = app
        .path()
        .resource_dir()
        .ok()
        .map(|p| p.join("_up_").join("core").join(name));
    if let Some(path) = packaged.filter(|p| p.exists()) {
        return Some(path);
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let sibling = parent.join("core").join(name);
            if sibling.exists() {
                return Some(sibling);
            }
        }
    }

    let local = workspace_root().join("core").join(name);
    if local.exists() {
        return Some(local);
    }

    None
}

fn candidate_core_script(app: &tauri::AppHandle) -> Option<PathBuf> {
    let packaged = app
        .path()
        .resource_dir()
        .ok()
        .map(|p| p.join("_up_").join("core").join("build_patch.py"));
    if let Some(path) = packaged.filter(|p| p.exists()) {
        return Some(path);
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let sibling = parent.join("core").join("build_patch.py");
            if sibling.exists() {
                return Some(sibling);
            }
        }
    }

    let local = workspace_root().join("core").join("build_patch.py");
    if local.exists() {
        return Some(local);
    }

    None
}

#[tauri::command]
fn pick_directory() -> Option<String> {
    rfd::FileDialog::new()
        .pick_folder()
        .map(|p| p.to_string_lossy().to_string())
}

#[tauri::command]
fn pick_file() -> Option<String> {
    rfd::FileDialog::new()
        .add_filter("JSON", &["json"])
        .pick_file()
        .map(|p| p.to_string_lossy().to_string())
}

#[tauri::command]
fn open_directory(path: String) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let mut cmd = Command::new("open");
    #[cfg(target_os = "windows")]
    let mut cmd = Command::new("explorer");
    #[cfg(target_os = "linux")]
    let mut cmd = Command::new("xdg-open");

    cmd.arg(path);
    cmd.spawn().map_err(|e| e.to_string())?;
    Ok(())
}

fn prepend_path(current: Option<std::ffi::OsString>, prefix: &Path) -> std::ffi::OsString {
    let mut paths = vec![prefix.to_path_buf()];
    if let Some(current) = current {
        paths.extend(std::env::split_paths(&current));
    }
    std::env::join_paths(paths).unwrap_or_default()
}

fn prepend_pythonpath(current: Option<std::ffi::OsString>, paths: &[PathBuf]) -> std::ffi::OsString {
    let mut merged = paths.to_vec();
    if let Some(current) = current {
        merged.extend(std::env::split_paths(&current));
    }
    std::env::join_paths(merged).unwrap_or_default()
}

#[tauri::command]
#[allow(clippy::too_many_arguments)]
fn run_patch(
    app: tauri::AppHandle,
    bundles2: String,
    outdir: String,
    mode: String,
    prices: Option<String>,
    price_field: String,
    season: Option<String>,
) -> Result<RunResult, String> {
    let core_binary = candidate_core_binary(&app);
    let script = candidate_core_script(&app);
    if core_binary.is_none() && script.is_none() {
        return Err("找不到补丁内核：缺少 poe2-price-patcher 或 build_patch.py".to_string());
    }

    let root = workspace_root();
    let thread_root = root
        .parent()
        .and_then(|p| p.parent())
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| root.clone());
    let work_dir = thread_root.join("work");
    let pydeps = work_dir.join("pydeps");
    let pypoe = work_dir.join("PyPoE");
    let ooz_dir = work_dir.join("ooz").join("build");

    let mut args = vec![
        "--bundles2".to_string(),
        bundles2,
        "--out".to_string(),
        outdir.clone(),
        "--hours".to_string(),
        "24".to_string(),
        "--price-field".to_string(),
        price_field,
    ];

    if mode == "local" {
        let prices = prices.ok_or_else(|| "本地模式缺少 prices.json".to_string())?;
        args.push("--prices".to_string());
        args.push(prices);
    } else {
        args.push("--fetch-prices".to_string());
    }

    if let Some(season) = season.filter(|s| !s.trim().is_empty()) {
        args.push("--season".to_string());
        args.push(season);
    }

    let mut command = if let Some(binary) = core_binary {
        Command::new(binary)
    } else {
        let script = script.expect("script checked above");
        let mut command = Command::new("python3");
        command.arg(script);
        if ooz_dir.exists() {
            command.env("PATH", prepend_path(std::env::var_os("PATH"), &ooz_dir));
        }
        let python_paths: Vec<PathBuf> = [pydeps, pypoe]
            .into_iter()
            .filter(|p| p.exists())
            .collect();
        if !python_paths.is_empty() {
            command.env(
                "PYTHONPATH",
                prepend_pythonpath(std::env::var_os("PYTHONPATH"), &python_paths),
            );
        }
        if work_dir.exists() {
            command.env("HOME", &work_dir);
        }
        command
    };
    command.args(args);

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Some(binary) = candidate_core_binary(&app) {
            if let Ok(metadata) = std::fs::metadata(&binary) {
                let mut permissions = metadata.permissions();
                permissions.set_mode(0o755);
                let _ = std::fs::set_permissions(binary, permissions);
            }
        }
    }

    let output = command.output().map_err(|e| e.to_string())?;
    let status = output.status.code().unwrap_or(-1);
    Ok(RunResult {
        ok: output.status.success(),
        status,
        stdout: String::from_utf8_lossy(&output.stdout).to_string(),
        stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        output_dir: outdir,
    })
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            pick_directory,
            pick_file,
            open_directory,
            run_patch
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri app");
}
