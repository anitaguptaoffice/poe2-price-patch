import { invoke } from "@tauri-apps/api/core";
import "./styles.css";

type RunResult = {
  ok: boolean;
  status: number;
  stdout: string;
  stderr: string;
  output_dir: string;
};

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

const logEl = $("log");
const summaryEl = $("summary");
const runBtn = $("run-btn") as HTMLButtonElement;
const openBtn = $("open-output") as HTMLButtonElement;

function value(id: string): string {
  return ($<HTMLInputElement>(id).value || "").trim();
}

function setLog(text: string) {
  logEl.textContent = text;
}

document.querySelectorAll<HTMLButtonElement>("[data-pick]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = button.dataset.pick!;
    const selected = await invoke<string | null>("pick_directory");
    if (selected) $<HTMLInputElement>(target).value = selected;
  });
});

document.querySelectorAll<HTMLButtonElement>("[data-pick-file]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = button.dataset.pickFile!;
    const selected = await invoke<string | null>("pick_file");
    if (selected) $<HTMLInputElement>(target).value = selected;
  });
});

runBtn.addEventListener("click", async () => {
  const mode = ($<HTMLSelectElement>("mode").value || "fetch") as "fetch" | "local";
  const bundles2 = value("bundles2");
  const outdir = value("outdir");
  const prices = value("prices");
  if (!bundles2 || !outdir) {
    summaryEl.textContent = "请先选择 Bundles2 和输出目录。";
    return;
  }
  if (mode === "local" && !prices) {
    summaryEl.textContent = "本地模式需要选择 prices.json。";
    return;
  }

  runBtn.setAttribute("disabled", "true");
  openBtn.setAttribute("disabled", "true");
  summaryEl.textContent = "生成中...";
  setLog("启动补丁生成...\n");

  try {
    const result = await invoke<RunResult>("run_patch", {
      bundles2,
      outdir,
      mode,
      prices: prices || null,
      priceField: ($<HTMLSelectElement>("price-field").value || "sell1"),
      season: value("season") || null,
    });
    setLog([result.stdout, result.stderr].filter(Boolean).join("\n\n"));
    summaryEl.textContent = result.ok ? "生成完成" : `生成失败，退出码 ${result.status}`;
    openBtn.toggleAttribute("disabled", !result.ok);
  } catch (error) {
    summaryEl.textContent = "运行失败";
    setLog(String(error));
  } finally {
    runBtn.removeAttribute("disabled");
  }
});

openBtn.addEventListener("click", async () => {
  const outdir = value("outdir");
  if (outdir) await invoke("open_directory", { path: outdir });
});
