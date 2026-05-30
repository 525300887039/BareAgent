# 修复 bash 工具 Windows 中文输出乱码（GBK/UTF-8 编码不匹配）

## Goal

修复 `run_bash`（`src/core/handlers/bash.py`）在 Windows 中文系统上返回乱码的问题。bash handler 用 `powershell -Command` 跑命令并**硬编码 `encoding="utf-8"`** 解码子进程输出；但 Windows PowerShell 5.1 在中文 locale 下用 **GBK(cp936)** 写 stdout/stderr（尤其 cmdlet 报错信息），导致 GBK 字节被当 UTF-8 解 → `errors="replace"` 全替换成 `�`（如 `字符串`→`�ַ���`）。需让两端编码对齐，使中文输出正确回传给 LLM。

## What I already know（已读代码定位）

- `bash.py:16-20`：Windows 走 `["powershell", "-NoProfile", "-Command", command]`，非 Windows 走 `["bash", "-lc", command]`。
- `bash.py:29-31`：`subprocess.run(..., text=True, encoding="utf-8", errors="replace")` —— **写死 UTF-8 解码**。
- `bash.py:55-65 _join_output`：对 bytes 分支也 `decode("utf-8", errors="replace")`（正常 text=True 路径走不到，仅 TimeoutExpired 可能给 bytes）。
- ASCII 在 GBK/UTF-8 一致 → 纯英文输出永远正常，**仅含中文时乱码**，故此前未暴露。
- 现有测试：`tests/test_tools.py` 已有 `test_bash_handler_runs_in_bound_workspace`、`test_bash_handler_decodes_binary_output_without_crashing`（用 `monkeypatch.setattr("src.core.handlers.bash.subprocess.run", fake_run)` mock，fake_run 捕获 args/kwargs 返回 `SimpleNamespace(stdout=..., stderr=..., returncode=...)`）。无独立 test_bash 文件。
- 本机就是中文 Windows + PowerShell 5.1 → 可在实现期真实复现并验证修复。

## Requirements（evolving）

- R1：Windows 分支让 PowerShell 以 UTF-8 写输出，与 Python 端 `encoding="utf-8"` 对齐——在 `-Command` 前置 `[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;`。
- R2：非 Windows（`bash -lc`）路径不变（Linux/mac 默认 UTF-8）。
- R3：补测试——(a) 跨平台单测 mock `subprocess.run` 捕获 argv，断言 Windows 命令含 UTF-8 前置、非 Windows 走 bash；(b) 中文输出回归（Windows-only，平台 skip 守卫，真实跑 PowerShell echo 中文断言无 `�`）。
- R4：实现期在本机真实复现乱码并验证修复后中文正确。

## Acceptance Criteria（evolving）

- [ ] 修复后，bash 工具跑出含中文的命令（如 PowerShell 中文报错、`Write-Output "中文"`），返回结果中文正确、无 `�`。
- [ ] 跨平台单测：Windows argv 含 `[Console]::OutputEncoding` UTF-8 前置；非 Windows 仍 `bash -lc`。
- [ ] 既有 bash 测试（workspace 绑定、binary decode）仍绿。
- [ ] `ruff check` 通过；改动文件已格式化（仅改动文件）。

## Definition of Done

- 新增/改动有 pytest 覆盖，默认 `pytest` 全绿。
- `ruff check src tests` 通过。
- 本机真实 E2E 验证中文不再乱码。

## Out of Scope（explicit）

- 不改输入/stdin 编码（仅修 stdout/stderr 回读乱码）。
- 不用 `chcp 65001`（会往 stdout 打印 "Active code page" 污染输出）。
- 不切到"按系统代码页(mbcs/GBK)解码"方案（会让真正吐 UTF-8 的命令如 curl 抓网页反而乱码）。
- 不处理罕见的"native exe 仍吐 GBK"边角（UTF-8 对齐已覆盖主流 cmdlet/现代工具/UTF-8 网页）。

## Decision (ADR-lite)

- **Context**：bash handler 写死 UTF-8 解码，但 Windows PS 5.1 中文系统用 GBK 写 stdout/stderr → 中文乱码。
- **Decision**：采用方案 A——Windows 分支在 `-Command` 前置 `[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;`，两端统一 UTF-8；保留 Python 端 `encoding="utf-8"`。测试两件套：跨平台 argv 单测 + Windows-only 真实 round-trip 回归（平台 skip）。
- **Consequences**：cmdlet 中文 + UTF-8 网页两头通吃，优于"按 GBK 解码"（后者会让 curl 抓的 UTF-8 网页乱码）；依赖 `[Console]::OutputEncoding` 在管道场景可设（实现期本机实测，必要时 try 兜底）；罕见 GBK-only native exe 不在覆盖内。

## Technical Notes

- 方案 A 实现：`["powershell", "-NoProfile", "-Command", "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command]`。`[Console]::OutputEncoding` 控制 PS 5.1 在 stdout 被重定向时的写出编码，及其解码 native 命令 stdout 的方式——设为 UTF8 后与 `encoding="utf-8"` 对齐。
- 潜在坑：`[Console]::OutputEncoding` setter 在无 console 时可能抛 IOException；subprocess 管道场景一般有 console，实现期本机实测确认（必要时 try 包裹）。
- 关键文件：`src/core/handlers/bash.py`、`tests/test_tools.py`。
