import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const venvPython = process.platform === "win32"
  ? join(root, ".venv", "Scripts", "python.exe")
  : join(root, ".venv", "bin", "python");
const [command, ...extraArgs] = process.argv.slice(2);

function run(executable, args) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(executable, args, { cwd: root, stdio: "inherit", shell: false });
    child.on("error", reject);
    child.on("exit", (code, signal) => resolveRun(code ?? (signal ? 1 : 0)));
  });
}

async function works(executable, args = ["--version"]) {
  try {
    return await run(executable, args) === 0;
  } catch {
    return false;
  }
}

async function bootstrapPython() {
  if (process.env.PYTHON) {
    if (await works(process.env.PYTHON)) return [process.env.PYTHON];
    throw new Error(`PYTHON 不可用：${process.env.PYTHON}`);
  }

  const candidates = process.platform === "win32"
    ? [["py", "-3"], ["python"]]
    : [["python3"], ["python"]];

  for (const candidate of candidates) {
    const [executable, ...args] = candidate;
    if (await works(executable, [...args, "--version"])) return candidate;
  }
  throw new Error("未找到 Python 3。请安装 Python 3.11+，或设置 PYTHON 为 Python 可执行文件路径。");
}

async function install() {
  if (!existsSync(venvPython)) {
    const [executable, ...args] = await bootstrapPython();
    const exitCode = await run(executable, [...args, "-m", "venv", ".venv"]);
    if (exitCode !== 0) process.exit(exitCode);
  }
  for (const args of [["-m", "pip", "install", "--upgrade", "pip"], ["-m", "pip", "install", "-e", ".[dev]"]]) {
    const exitCode = await run(venvPython, args);
    if (exitCode !== 0) process.exit(exitCode);
  }
}

function requireVenv() {
  if (!existsSync(venvPython)) {
    throw new Error("未找到 .venv。请先运行 npm run install。");
  }
}

try {
  switch (command) {
    case "install":
      await install();
      break;
    case "start":
      requireVenv();
      process.exitCode = await run(venvPython, ["-m", "feishu_claude_mvp.main", ...extraArgs]);
      break;
    case "test":
      requireVenv();
      process.exitCode = await run(venvPython, ["-m", "pytest", ...extraArgs]);
      break;
    default:
      throw new Error("用法：node scripts/python-runner.mjs <install|start|test>");
  }
} catch (error) {
  console.error(`错误：${error.message}`);
  process.exitCode = 1;
}
