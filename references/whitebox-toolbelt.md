# 白盒理解工具矩阵

目标：用最少的工具拿到“结构、契约、调用链、状态、历史、运行行为”，而不是逐文件硬读。**先探测，再选路；缺工具就用最小替代，不要停摆。**

## 1. 工具探测

```bash
# 一次性盘点（也可用 sf.py probe 自动采集）
for t in cc c++ cmake make ninja bazel meson clangd clang-tidy cppcheck scan-build \
         infer semgrep codeql ctags cscope gtags tree-sitter gdb lldb valgrind \
         perf strace ltrace gcov lcov llvm-cov nm objdump readelf ldd strings \
         bear compiledb doxygen sphinx-build mkdocs shellcheck ruff mypy pyright \
         pylint pytest hypothesis afl-fuzz honggfuzz cargo clippy go node npm pnpm; do
  command -v "$t" >/dev/null 2>&1 && printf '%-14s %s\n' "$t" "$(command -v "$t")"
done
```

记录到 `<run>/01-context/tooling.md`：可用/不可用、版本、选它做什么、缺了用什么替代。**不要**未经用户同意安装重型工具或联网拉依赖。

## 2. 代码结构 / 构建

| 目的 | 首选 | 命令/产物 | 缺失替代 |
|---|---|---|---|
| 编译数据库 | CMake / Bear / compiledb | `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`、`bear -- make` | 从 Makefile/构建日志提取 `-I/-D` |
| 依赖/目标图 | CMake graphviz、Bazel query | `cmake --graphviz=deps.dot`、`bazel query 'deps(//...)'` | `grep` 构建文件 + `ldd` |
| 入口/模块 | 目录结构、main、导出符号 | `git ls-files`、`nm -D`、`readelf -Ws` | `grep -R "int main"`、README |
| 调用图 | clangd/LSP、ctags、cscope | `ctags -R`、`cscope -R` | `git grep -n "func("` + 人工 |
| 代码检索 | ripgrep/git grep | `git grep -n <symbol>`、`git log -S<symbol>` | DSH grep/glob |
| 变更历史 | git | `git log --oneline -- <path>`、`git blame`、`git log -S` | 无 |
| 文档 | Doxygen/Sphinx/MkDocs | `doxygen Doxyfile`、`sphinx-build` | 直接读 docs/ |

## 3. 契约 / 静态分析

| 目的 | 首选 | 命令 | 缺失替代 |
|---|---|---|---|
| 编译告警 | 编译器 | `-Wall -Wextra -Werror=return-type` | 无 |
| 路径敏感分析 | GCC `-fanalyzer` | `gcc -fanalyzer -c file.c` | 人工审查错误分支 |
| C/C++ lint | clang-tidy/cppcheck | `clang-tidy file.cpp -- -I...`、`cppcheck --enable=all .` | 编译器 + grep 模式 |
| 安全扫描 | semgrep/CodeQL | `semgrep --config auto .` | 针对 sink 的 grep（strcpy/system/popen/...） |
| 符号/ABI | nm/readelf | `nm -D --defined-only lib.so`、`readelf -d lib.so` | `strings` + `ldd` |
| 结构体布局 | pahole/编译器 | `pahole -C Type bin` | `offsetof` 探针 |
| 头文件一致性 | 编译测试 | 分别用 C/C++ 编译最小 TU | 手工比对声明 |
| 文档契约 | 头文件/doc | 提取 `@return/@param/@note` 到契约表 | 读 README/API 文档 |

## 4. 动态行为

| 目的 | 首选 | 命令 | 缺失替代 |
|---|---|---|---|
| 系统调用 | strace | `strace -f -tt -o trace.log cmd` | `LD_DEBUG=libs`、日志 |
| 库加载 | ldd/LD_DEBUG | `ldd bin`、`LD_DEBUG=libs cmd` | `readelf -d` |
| 崩溃 | gdb/lldb | `gdb --args bin`、`bt` | `catchsegv`、core + `readelf -n` |
| 内存错误 | ASan/valgrind | `-fsanitize=address,undefined`、`valgrind --leak-check=full` | 资源计数 + 自写探针 |
| 数据竞争 | TSan | `-fsanitize=thread` | 压力 + 计数对账 + 人工锁审查 |
| 性能 | perf | `perf stat/record` | `time`、`/proc`、自写计时 |
| 资源 | /proc | `VmRSS`、`Threads`、`fd`、`/proc/<pid>/status` | `ps`、`lsof` |
| 覆盖率 | gcov/lcov/llvm-cov | `--coverage` + `gcov` | 手工路径清单 |

## 5. 语言速查

- **C/C++**：compile_commands + clangd/clang-tidy（若有）；`-fanalyzer`；ASan/UBSan/TSan；`nm/readelf`；`gcov`；`libFuzzer`/AFL++（若有）。
- **Python**：`python -m compileall`、`ast` 自写脚本；ruff/mypy/pyright/pylint；pytest + hypothesis；coverage.py；`faulthandler`、`tracemalloc`。
- **Go**：`go vet`、`staticcheck`、`go test -race`、`go test -fuzz`、`go tool pprof`、`go test -cover`。
- **Rust**：`cargo clippy`、`cargo test`、`cargo fuzz`、`miri`、`cargo llvm-cov`。
- **Java**：SpotBugs、Error Prone、JUnit、Jazzer、JFR。
- **JS/TS**：`tsc --noEmit`、ESLint、Vitest/Jest、Playwright、fast-check、`--inspect`。
- **Shell**：`bash -n`、shellcheck；`set -x`；`strace`。

## 6. 代码理解工作流

1. **构建系统** → 语言/目标/依赖/产物。
2. **公共边界** → 头文件/导出符号/CLI 参数/HTTP 路由/配置 schema。
3. **核心路径** → 入口 → 关键函数 → 资源/状态 → 回调/线程。
4. **契约** → 参数校验、错误码、状态机、线程安全、限制。
5. **历史** → 测试/CI/issue/PR/changelog；找“曾经坏过”的地方。
6. **动态验证** → 最小探针/日志/断点/时序，确认静态推断。
7. **风险模型** → 爆炸半径 × 触发概率 × 可测性。

## 7. 无工具时的最小替代

- 结构：`git ls-files` + `grep` 入口/导出 + README。
- 调用：`grep -R "name("` + 人工跟 3 层。
- 契约：头文件注释 + 文档 + 运行时探针。
- 动态：`strace` + `/proc` + 日志 + 自写 C/Python 探针。
- 崩溃：`dmesg`/plog/core + `readelf -n`；无 gdb 时用 `addr2line`。
- 性能：`time` + `/proc/<pid>/stat` + 自写计时/计数。
- 覆盖率：手工路径清单 + 计数器，不假装有覆盖率。

## 8. 输出归档

- 工具版本、命令、参数、原始输出放 `01-context/` 或 `05-evidence/`。
- 静态分析结果要标“静态推断”，不能直接当 finding；需要运行验证。
- 大输出只保留摘要 + 原始文件路径；报告里引用路径而不是粘贴全文。
- 不把目标仓的构建产物写回目标仓；快照构建在 run 目录。
