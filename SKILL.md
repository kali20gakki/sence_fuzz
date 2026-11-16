---
name: scene-fuzz
description: 当用户要求对某个代码仓或特性做白盒自动测试、场景化 fuzz、质量验证、加压回归、问题定位，或要求产出可复现测试与详细报告时使用；强调先建立使用场景/关键性画像、再理解代码与文档、再设计场景，所有结论必须来自真实运行证据。
whenToUse: 适用于有可构建/可运行环境、且需要超越现有单测，从真实使用场景、稳定性、性能与高危路径构造测试的场景。
---

# scene-fuzz：场景化白盒自动测试

本 skill 把“先理解使用场景，再做白盒测试”固化为可重复流程。它不是普通单测补写，也不是只做静态审查；它要求把业务真实跑起来，用证据说话。

**资源根目录**：加载时给出的 `SKILL.md` 所在目录。默认安装路径 `/home/zw/scene_fuzz/.dsh/skills/scene-fuzz`；若通过用户级软链加载，等价路径为 `/root/.dsh/skills/scene-fuzz`。以下用 `<skill>` 表示该目录。

## 1. 什么时候用 / 不用

适用：

- 用户要求“测一下这个仓库/这个特性”“做一轮白盒 fuzz”“加压/边界/并发/长稳”“找出高危问题并给报告”。
- 已有单测但覆盖不到真实使用方式，或历史用例需要演进。
- 需要可复现的测试代码、实验数据和结论。

不适用：

- 只需要读代码解释逻辑（直接用 grep/read 回答即可）。
- 没有构建/运行环境，且用户不接受 env-blocked 结论。
- 替代安全评审、形式化验证或人工专家判断。

## 2. 输入与输出

输入：

- `target`：目标仓或特性路径（必须可构建/可运行）。
- `run-id`：可选；默认 `<target>-<yyyymmdd>-<sha7>`。
- 历史运行：可选；默认扫描 `<runs-root>/*/`。

输出（全部在 run 目录，不写目标仓）：

```text
<run>/
  run.json
  REPORT.md
  ledger.jsonl
  00-run-meta/       # env/tools/resources/hashes
  01-context/        # 场景画像、代码地图、契约、历史、风险、工具、知识源
  02-scenarios/      # 场景矩阵、case-list、seeds
  03-harness/        # 可复现测试代码与构建日志
  04-cases/<id>/     # case.json、run.sh、notes.md
  05-evidence/<id>/attempt-N/
  06-analysis/       # triage、findings
  07-report/         # coverage/perf/soak/evidence-index
  08-retrospective/  # skill 复盘、下次种子
  09-loop/           # state.json / iterations.jsonl / frontier.md / loop-report.md
```

## 3. 铁律（不可协商）

1. **先画像后设计**：`01-context/usage-context.md` 未完成，不得设计用例。
2. **先理解后设计**：`01-context/` 其余文档未完成，不得设计用例。
3. **先基线后加压**：baseline 未通过，不得把后续失败归因于产品。
4. **所有结论必须有运行证据**：落到 `05-evidence/<case>/attempt-N/`；静态阅读、文档、KG 只能放“假设/设计输入”。
5. **目标仓只读**：构建、harness、日志全部在 run 目录；开始/结束各记一次 `git status`。
6. **在线库/注入式组件以“不打断宿主任务”为第一 oracle**：宿主输出正确、退出码正常、无信号/挂起，优先于“功能点是否全覆盖”。
7. **每个用例记录**：命令、cwd、环境、资源、退出码、信号、耗时、超时、峰值资源；至少重复 2 次，或说明为何不能。
8. **环境失败必须自证**：baseline + 最小命令 + 对照实验；不确定标 `INCONCLUSIVE`。
9. **允许负结论**：没有发现缺陷就写“未复现/未发现”，并写残余风险；不允许为了有发现而报 bug。
10. **密钥只从环境变量读取**：不写入 skill、case、日志、报告、git。
11. **loop 模式必须有状态**：没有 `09-loop/state.json` 或没有记录 `next`/`stop` 原因，不得声称“已持续演进”。

## 4. 六阶段流程

### Phase 0：立项、快照、基线

1. 用 `sf.py init` 建 run；用 `sf.py probe` 采集 host、git、工具链、资源、环境（脱敏）、hash。
2. 将目标源码快照到 `03-harness/target-src/`（排除 build/依赖缓存/大测试数据），在快照里构建；记录构建日志和产物 sha256。
3. 跑“已知应该通过”的 baseline（官方样例/现有测试/最小业务路径）。baseline 失败先判环境。

Gate：`00-run-meta/` 完整 + baseline 通过。

### Phase 1：使用场景画像 + 白盒理解

先读 `references/usage-context.md`，建立 `01-context/usage-context.md`：artifact 类型、集成方式、关键性、失败爆炸半径、生命周期、并发、性能预算、边界契约、兼容矩阵、可观测性、不变量。

然后用工具（`references/whitebox-toolbelt.md`）完成：

- `repo-map.md`：目录/模块/构建系统/入口/公共 API/依赖/数据流。
- `contracts.md`：文档与头文件中的参数、返回码、状态机、线程安全、限制、默认值；标出文档与代码可能不一致处。
- `usage-scenarios.md`：从 README/docs/samples/tests/issues/CI/领域 KG 提取的真实用户任务、调用序列、失败模式。
- `history-review.md`：历史单测/ST/UT/CI 报告/旧 run；哪些 oracle 可复用、哪些脆弱、哪些缺口。
- `risk-model.md`：按“爆炸半径 × 触发概率 × 可测性”排序的高危模块/路径。
- `tooling.md`：本机可用/不可用工具、所选工具、缺失替代。
- `domain-kg.md`（可选）：领域 KG/MCP 查询记录（query/id/score/source/date/用途）。

Gate：八份文档完成；每个风险有代码定位；每个场景有来源。

### Phase 2：场景设计

按 `references/scenario-taxonomy.md` 设计 case matrix：

- 契约/边界：NULL、0、SIZE_MAX、非法枚举、版本、短缓冲、重复调用。
- 状态机/生命周期：初始化、重入、重复终止、终止后使用、并发终止。
- 数据正确性：计数、顺序、关联 id、时间戳、完整性、对账。
- 并发：多线程、回调重入、生产者/消费者、背压、竞态。
- 资源：RSS、线程、fd、句柄、缓冲、日志量、泄漏。
- 故障注入：依赖缺失、权限、设备离线、磁盘满、慢消费者、信号。
- 负载/长稳：ramp、soak、吞吐/延迟/开销漂移。
- 稳定性/fail-safe：宿主任务是否被打断、是否 crash/abort/挂起、是否可降级。
- 兼容/ABI：头文件/库/版本/平台/多客户端/共存。
- 文档/使用场景偏差：文档承诺 vs 实际行为。

每个 case 必须写：hypothesis、whitebox_refs、oracle、资源、lineage（new/reuse/evolve）。P0 是“能证明核心不变量/最高爆炸半径”的用例；不要用大量低价值用例冲数量。

Gate：`02-scenarios/scenario-matrix.md` + `case-list.json` 完成；P0 能映射到不变量。

### Phase 3：可执行化

- 每个 case 一个目录：`case.json`、`run.sh`（或编译脚本）、`notes.md`。
- harness 优先复用仓库原生测试/样例；自写 harness 放 `03-harness/src/`，用 `Makefile`/脚本构建，记录编译命令。
- 隔离：每个 case 独立 cwd、日志、临时目录、环境变量；不写目标仓。
- 资源声明：`resources`（如 `npu:0`、`port:8080`、`exclusive`）。
- 超时：进程组级 timeout；崩溃/挂起可回收。
- 证据：stdout/stderr/result.json/artifacts；summary 用 JSON，避免日志洪水。

Gate：每个 P0 case 可一条命令复现；baseline 与 case 使用同一构建产物。

### Phase 4：执行与分诊

- 用 `sf.py run` 单跑，或 `sf.py run-suite --jobs N` 并行；冲突资源自动串行，性能/长稳必须独占。
- 每个 case 至少 2 次；压力类 3 次；记录分布、方差、是否受并行干扰。
- 分类：PASS / PRODUCT_BUG / ENV_BLOCKED / TEST_BUG / INCONCLUSIVE。
- 异常先最小化复现；崩溃保留 exit signal、stderr、plog；不保留巨型 core。
- 环境失败：baseline + 最小命令 + 对照；数据丢失：计数 + dropped 对账。

Gate：P0 全部有结论；异常有最小复现或明确“无法最小化”的原因。

### Phase 5：演进循环（Evolution Loop）

一轮测试结束不等于工作结束。按 `references/evolution-loop.md` 进入结果驱动的循环：

```text
观察最新结果 → 决策（继续/升级/收敛/最小化/停止）
→ 生成下一代（一个主轴 + 可选一个副轴）→ 执行
→ 记录 case/结果/证据/lineage/next → 更新 frontier → 回到观察
```

- **状态**：`09-loop/state.json`、`iterations.jsonl`、`frontier.md`、`loop-report.md`。
- **命令**：`evolve.py init/next/record/status/frontier`。
- **决策表**：失败→最小化；dropped>0→找阈值；host 失败→稳定性隔离；开销超预算→归因；全 PASS→升级未到上限的轴；连续 3 代无新信号→换轴或停止。
- **变异算子**：load ×10、threads +4、buffer ÷2、soak ×2、cycles ×2、故障组合、边界、差分；一次只改一个主轴，记录因子。
- **记录纪律**：每个新一代必须写 `lineage`（new/reuse/evolve:<case>）和“改了什么、因子多少”；不允许重复同一负载点。
- **停止**：预算耗尽、连续 3 代无新信号、风险/资源上限、目标不变量已充分证明、环境不可恢复；停止时写 `loop-report.md`。
- **沉淀**：通过的高压点成为新 baseline；失败点成为 regression；可复用 seed 进 `corpus/<target>/seeds.jsonl`。

Gate：至少完成 1 次“观察→决策→下一代→record”的闭环；`09-loop/state.json` 有 next 或 stop 原因。

### Phase 6：报告与沉淀

- 写 findings（只写有证据的），更新 `06-analysis/triage.md`。
- 写 `REPORT.md`（按 `references/report-standard.md`），生成 coverage/perf/soak 表。
- 跑 `sf.py check`；修复缺失证据/章节/重叠资源。
- 写 `08-retrospective/skill-review.md`：哪些流程有效、哪些缺口、下次怎么改；必要时回写 skill。
- 将可复用 seed 提升到 `corpus/<target>/seeds.jsonl`。

Gate：`check` 通过；报告每个高危主题有结论；无密钥泄漏。

## 5. 命令速查

```bash
python3 <skill>/scripts/sf.py init --target <repo> --run-dir <runs>/<run-id>
python3 <skill>/scripts/sf.py probe --run-dir <run>
python3 <skill>/scripts/sf.py run --run-dir <run> --case <run>/04-cases/C-01/case.json
python3 <skill>/scripts/sf.py run-suite --run-dir <run> --case-list <run>/02-scenarios/case-list.json --jobs 4
python3 <skill>/scripts/sf.py check --run-dir <run>

# 演进循环
python3 <skill>/scripts/evolve.py init --run-dir <run> [--known-failures C-04]
python3 <skill>/scripts/evolve.py next --run-dir <run>
python3 <skill>/scripts/evolve.py record --run-dir <run> --case C-026 --axis concurrency --change "threads 4->8"
python3 <skill>/scripts/evolve.py status --run-dir <run>
```

## 6. 并行执行

- case.json 的 `resources` 是独占资源；同一资源同一时刻只跑一个 case。
- `exclusive: true` 表示对 `resources` 声明的资源独占；未声明任何资源时才是全局独占（等所有 case 结束）。
- 性能、长稳、共享设备、固定端口、全局 profiler、会改系统状态的 case 必须独占其资源；性能/长稳不应与其他负载共享同一设备。
- 并行只用于互不共享状态的 case；并行后对关键计数做串行抽样复跑。
- 证据记录 `resources_held` 与 `concurrent_with`；`check` 会检测独占资源重叠。

## 7. 常见坑

- 把“文档这么说”当成“实际如此”：必须跑。
- baseline 没跑就压测：失败无法归因。
- 只看返回值不看宿主任务：在线库的致命问题是“返回了错误但进程挂了/卡了”。
- 消费者太慢导致丢数：区分预期丢弃与未记账丢失。
- 日志打印太多改变时序：summary 落 JSON，stdout 限量。
- 并行共享设备/端口/日志：结果互相污染。
- 历史 oracle 依赖固定样例/固定计数：先确认版本与构建，再复用。
- 密钥写进 case/日志/报告：只在环境变量里，产物里只留指纹。
- 只写“通过”：负结论也要写清测试范围与残余风险。

## 8. 资源上限

- 默认单 case timeout 120s；长稳/性能单独配置，最长不超过用户预算。
- 并发默认 `--jobs 1`；显式开启，先从 2–4 开始。
- 单 case 日志上限 100MB；超出截断并标注。
- 线程/迭代数有上限；压力 ramp 逐步加，不一次性打满。
- core dump 默认关闭（`ulimit -c 0`），避免磁盘打满。

## 9. 参考

- `references/usage-context.md`：场景/关键性画像与不变量。
- `references/whitebox-toolbelt.md`：工具矩阵与无工具替代。
- `references/knowledge-sources.md`：文档/历史/领域 KG 接入与引用纪律。
- `references/scenario-taxonomy.md`：设计维度、边界清单、oracle、fuzz 演进。
- `references/evidence-and-triage.md`：证据、分类、最小化、finding 规范。
- `references/report-standard.md`：报告章节、severity、写作规则。
- `references/evolution-loop.md`：观察/决策/变异/预算/停止/前沿/记录规范。
