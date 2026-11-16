# scene-fuzz

[![GitHub Repo](https://img.shields.io/badge/GitHub-kali20gakki%2Fsence__fuzz-181717?logo=github)](https://github.com/kali20gakki/sence_fuzz)
[![Works with](https://img.shields.io/badge/Works%20with-DSH%20%7C%20Claude%20Code%20%7C%20Codex%20%7C%20OpenCode-0A7B83)](#安装)
[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python)](https://www.python.org/)

一个**场景化白盒自动测试 Skill**：先建立使用场景画像，再理解代码与文档，再设计边界、压力、并发、故障与长稳用例；所有结论必须由真实运行证据支撑，并通过 Evolution Loop 持续加压、演进测试方向。

它面向“在线库/服务/CLI/插件”这类真实软件，而不是只补几条单测。核心目标是把下面这条链路变成可重复的工程流程：

```text
场景画像 → 白盒理解 → 场景设计 → 可执行化 → 运行/分诊 → 演进循环 → 报告/沉淀
```

---

## 为什么不是普通单测

普通单测回答“函数对不对”；scene-fuzz 回答：

- 这个组件在**真实使用方式**下会不会打断宿主任务？
- 参数边界、并发、慢消费者、依赖缺失、长稳运行会不会出问题？
- 性能开销、RSS/线程/fd/日志量是否可解释、可持续？
- 现有测试的 oracle 是否还成立？哪些缺口需要加压？
- 这一轮通过后，**下一轮应该往哪里加压**？

因此它强调三件事：

1. **先画像后设计**：先写清 artifact 类型、集成方式、关键性、失败爆炸半径、生命周期、并发模型、性能预算、边界契约。
2. **先基线后加压**：baseline 未通过，不把失败归因于产品。
3. **结果驱动循环**：每代记录 frontier/signals/next；不重复已通过的点，直到预算/风险/收益停止。

---

## 安装

仓库根目录就是 skill 目录（包含 `SKILL.md`、`references/`、`scripts/`、`assets/`）。`npx skills` 可以直接识别本仓库，DSH 也可以用目录方式安装。

### 推荐：skills CLI

先确认仓库能被识别：

```bash
npx -y skills add kali20gakki/sence_fuzz --list
```

输出里能看到 `scene-fuzz` 即可安装。

#### Codex

```bash
npx -y skills add kali20gakki/sence_fuzz --skill scene-fuzz -a codex -g -y
```

#### Claude Code

```bash
npx -y skills add kali20gakki/sence_fuzz --skill scene-fuzz -a claude-code -g -y
```

#### OpenCode

```bash
npx -y skills add kali20gakki/sence_fuzz --skill scene-fuzz -a opencode -g -y
```

`-g` 表示安装到用户级 skills 目录，`-y` 表示跳过交互确认；去掉 `-g` 则按当前 Agent 的项目级目录安装。

#### DSH（DeepSeek Harness）

`npx skills` 目前还没有 DSH 目标，DSH 用下面的目录方式安装；DSH 会扫描 skills 目录并热加载。

### DSH（目录安装）

用户级（推荐）：

```bash
git clone https://github.com/kali20gakki/sence_fuzz.git ~/.dsh/skills/scene-fuzz
# 或者
cp -r ./scene-fuzz ~/.dsh/skills/scene-fuzz
```

项目级：

```bash
cp -r ./scene-fuzz <your-repo>/.dsh/skills/scene-fuzz
```

DSH 也会扫描 `~/.agents/skills`、`<projectRoot>/.agents/skills`；放在任一 skills 根目录即可。验证：新会话里应能看到 `scene-fuzz`，或直接说“用 scene-fuzz 测一下这个仓库”。

### 手动安装（不使用 skills CLI）

Claude Code：

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)" ~/.claude/skills/scene-fuzz
```

Codex：

```bash
mkdir -p ~/.codex/skills
ln -s "$(pwd)" ~/.codex/skills/scene-fuzz
```

OpenCode：

```bash
mkdir -p ~/.config/opencode/skills
ln -s "$(pwd)" ~/.config/opencode/skills/scene-fuzz
```

> 不同 Agent 的 skills 根目录可能不同；只要目录里包含 `SKILL.md`，并按 Agent 的加载规则放置即可。

### 更新

使用 skills CLI：

```bash
npx -y skills check
npx -y skills update
```

或重新安装一次最新版：

```bash
npx -y skills add kali20gakki/sence_fuzz --skill scene-fuzz -a codex -g -y
```

DSH / 手动安装：

```bash
git -C ~/.dsh/skills/scene-fuzz pull
```

### 前置依赖

- Python 3.8+（runner 只用标准库）。
- Bash、Linux（`sf.py` 使用 `/proc`、进程组和 `flock`；其他平台可自行适配）。
- 目标仓的构建/运行环境（编译器、测试框架、设备/端口/权限等）。
- 可选：领域知识图谱/MCP 的 API Key；不配置也能工作。

---

## 快速上手

### 1. 让 Agent 开始

```text
用 scene-fuzz 测一下 /path/to/repo，重点看在线库的稳定性、并发和长稳。
```

Agent 会按 `SKILL.md` 执行：

1. 建 run 目录，`sf.py probe` 采集环境/工具/资源/密钥指纹。
2. 在 run 目录内做源码快照并构建，跑 baseline。
3. 写 `01-context/` 场景画像、代码地图、契约、历史、风险、工具、知识源。
4. 设计 case matrix，生成 `04-cases/` 与 harness。
5. 运行/分诊，证据落 `05-evidence/`。
6. 进入 Evolution Loop，生成下一代。
7. 写 `REPORT.md`，跑 `sf.py check`，沉淀复盘与种子。

### 2. 手动执行

```bash
# 初始化 run
python3 scripts/sf.py init --target /path/to/repo --run-dir /path/to/run

# 采集环境、工具、资源、hash、key 指纹
python3 scripts/sf.py probe --run-dir /path/to/run

# 单用例
python3 scripts/sf.py run --run-dir /path/to/run \
  --case /path/to/run/04-cases/C-001/case.json

# 资源感知并行 suite
python3 scripts/sf.py run-suite --run-dir /path/to/run \
  --case-list /path/to/run/02-scenarios/case-list.json --jobs 4

# 证据门禁（有 loop 时加 --require-loop）
python3 scripts/sf.py check --run-dir /path/to/run --require-loop
```

### 3. 演进循环

```bash
python3 scripts/evolve.py init --run-dir /path/to/run [--known-failures C-004]
python3 scripts/evolve.py next --run-dir /path/to/run
python3 scripts/evolve.py record --run-dir /path/to/run \
  --case C-010 --axis concurrency --change "threads 4->8"
python3 scripts/evolve.py status --run-dir /path/to/run
python3 scripts/evolve.py frontier --run-dir /path/to/run
```

`evolve.py` 只读取 runner 已记录的证据，不会编造结果；它输出下一代的轴、参数和理由。

---

## 这个 Skill 能做什么

### 1. 使用场景画像

`references/usage-context.md` 把“代码”翻译成“使用约束”：

- artifact 类型、集成方式（linked/preload/sidecar/plugin…）
- 关键性：online / offline / batch
- 失败爆炸半径、生命周期、并发模型
- 性能预算、边界契约、兼容矩阵、可观测性
- I1–I10 不变量：宿主成功、无 crash、无挂起、开销有界、资源有界、数据可对账、边界可返回、可共存、长稳、生命周期安全

### 2. 白盒理解工具矩阵

`references/whitebox-toolbelt.md` 先探测再选路：

- 编译数据库、调用图、ABI/符号、动态观测、sanitizer、覆盖率、fuzz、测试历史
- 有 clangd/clang-tidy/gdb/valgrind 就用；没有就用 `grep + 编译器 analyzer + nm/readelf + strace + /proc + 自写探针`
- 记录工具版本、命令、输出与替代方案，不因缺工具停摆

### 3. 可选领域知识源

`references/knowledge-sources.md` 支持接入领域 KG/MCP（例如 Ascend/Kunpeng 的 kg-tools）：

- 查业务场景、API 契约、错误码、跨仓调用链
- 记录 query / node id / score / source / date
- **KG 只作设计输入，不作为运行证据**
- 不可用时降级到本地 docs/tests，并标 `[unverified]`
- 密钥只从环境变量读取，不落盘

### 4. 证据门禁

每个 case 的执行都记录：

- 命令、cwd、环境、资源、退出码/信号、耗时、超时
- 峰值 RSS、线程数、stdout/stderr
- `result.json` + `ledger.jsonl`

`sf.py check` 校验：

- P0 是否有 attempt/证据
- `01-context/` 八件套是否齐全
- `REPORT.md` 章节是否完整
- finding 的证据路径是否存在、是否越界
- 最新 attempt 是否有资源重叠
- 是否误把密钥写进产物
- loop 模式是否有 state/iterations/frontier/next

### 5. 资源感知并行

case 声明 `resources`（例如 `npu:0`、`gpu:1`、`port:8080`）和 `exclusive`：

- 同一资源同一时刻只跑一个 case
- 未声明资源的 `exclusive` 是全局独占
- 性能/长稳必须独占其设备
- `result.json` 记录 `resources_held` 与 `concurrent_with`

### 6. Evolution Loop

`references/evolution-loop.md` + `scripts/evolve.py` 提供结果驱动循环：

```text
观察 → 决策 → 生成下一代（一个主轴 + 可选副轴）
→ 执行 → record → 更新 frontier → 继续/停止
```

- 决策表：失败→最小化；dropped>0→找阈值；host 失败→稳定性隔离；开销超预算→归因；全 PASS→升级
- 变异算子：load ×10、threads +4、buffer ÷2、soak ×2、cycles ×2、故障组合、边界、差分
- 前沿：max load / threads / soak / min buffer / overhead / RSS growth
- 停止：预算耗尽、连续 3 代无新信号、风险/资源上限、目标不变量已证明、环境不可恢复
- 记录：`09-loop/state.json`、`iterations.jsonl`、`frontier.md`、`loop-report.md`

### 7. 报告与沉淀

- `REPORT.md` 固定 13 节：结论摘要、范围方法、场景画像、系统理解、环境基线、覆盖矩阵、稳定性/性能/长稳、高危分析、负结果、局限、复现、并行/干扰、后续建议
- finding 只写有运行证据的；静态/KG 推断放“假设/残余风险”
- `08-retrospective/` 记录 skill 自身缺口与修改
- 可复用参数进 `corpus/<target>/seeds.jsonl`

---

## 目录结构

```text
scene-fuzz/
├── SKILL.md                      # Skill 正文（Agent 加载入口）
├── README.md
├── references/
│   ├── usage-context.md          # 使用场景/关键性画像
│   ├── whitebox-toolbelt.md      # 白盒工具矩阵与替代
│   ├── knowledge-sources.md      # 本地文档/历史/领域 KG
│   ├── scenario-taxonomy.md      # 场景/边界/并发/故障/性能设计维度
│   ├── evidence-and-triage.md    # 证据、分类、最小化、finding
│   ├── report-standard.md        # 报告规范
│   └── evolution-loop.md         # 演进循环/前沿/停止
├── scripts/
│   ├── sf.py                     # init/probe/run/run-suite/check
│   ├── evolve.py                 # init/next/record/status/frontier
│   └── selftest.sh
└── assets/
    ├── case.json.tmpl
    ├── finding.md.tmpl
    ├── REPORT.md.tmpl
    ├── loop-state.json.tmpl
    ├── frontier.md.tmpl
    └── seeds.jsonl.tmpl
```

运行产物（`runs/`、`corpus/`、证据、目标源码快照）不属于 skill，不应提交到本仓库。

---

## 一个 case 长什么样

`assets/case.json.tmpl`：

```json
{
  "id": "C-010",
  "title": "8 线程并发下不丢记录",
  "priority": "P0",
  "risk": "concurrency",
  "lineage": "evolve:C-008",
  "hypothesis": "并发提高后可能出现计数丢失或挂起",
  "whitebox_refs": ["<path>:<line>"],
  "requires": ["device"],
  "resources": ["device:0"],
  "exclusive": true,
  "env": {"DEVICE_VISIBLE": "0"},
  "run": {"cmd": "bash run.sh", "cwd": ".", "timeout_sec": 180},
  "repeat": 2,
  "oracle": "宿主输出正确；count == N；dropped == 0；无信号/超时"
}
```

`run.sh` 负责目标相关的构建/执行；`sf.py` 负责超时、证据、ledger、资源与并行。

---

## 设计原则

- **目标仓只读**：构建、harness、日志都在 run 目录；开始/结束各记一次 `git status`。
- **在线组件第一 oracle 是“宿主任务不被打断”**：输出正确、退出码正常、无信号/挂起。
- **允许负结论**：没有发现就写“未复现/未发现”，并写残余风险；不为有发现而报 bug。
- **一次只改一个主轴**：loop 每代记录变更因子，避免多参数同时变导致无法归因。
- **性能/长稳必须独占**：不与其他负载共享设备；报告方差与干扰。
- **密钥零落盘**：只从环境变量读取；日志/报告只留指纹。
- **不做破坏性系统操作**：device reset、驱动卸载、影响他人的操作不在默认范围。

---

## 适用与不适用

适用：

- 有可构建/可运行环境的 C/C++、Python、Go、Rust、Java、JS/TS 项目
- 在线库/SDK、服务/daemon、CLI、插件/注入组件
- 已有单测但覆盖不到真实场景，或需要持续加压/回归

不适用：

- 只需要解释代码逻辑（直接读代码即可）
- 没有运行环境且用户不接受 env-blocked 结论
- 替代安全评审、形式化验证或人工专家判断

---

## 已验证的能力

该 skill 已在真实 C/C++ 在线库场景中做过端到端验证，覆盖：

- 契约/边界、生命周期/finalize、Activity/数据面、Callback、PM 状态机、Python API
- 5000→32,000 kernels、4→12 threads、8KB→4KB buffer、120s→300s soak
- 故障矩阵（无 preload、非法参数、无回调、并发 finalize）
- 50 seeds 状态序列 fuzz
- 并行执行与证据门禁
- Evolution Loop 第 2–5 代自动前移

验证数据与报告留在独立 run 目录，不随 skill 仓库发布。

---

## 维护

```bash
# 自测 runner + loop
bash scripts/selftest.sh

# 编译检查
python3 -m py_compile scripts/sf.py scripts/evolve.py
```

如果目标 Agent 的 skills 根目录不同，只需把整个目录放到对应位置；更新时 `git pull` 或重新复制即可。

---

## 仓库

- GitHub：<https://github.com/kali20gakki/sence_fuzz>
- Skill 名称：`scene-fuzz`
- 入口：`SKILL.md`
