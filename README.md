# 发酵罐温度日志质控系统（Flask + SQLite + NDJSON）

把发酵设备持续追加的 NDJSON 温度日志，按“只增不丢、非法整轮拒绝”的规则整理成
质控记录，提供网页查看、CSV 下载，并附带可演示**半行**与**日志轮转**的本地示例。

## 一、设备日志模型

设备只维护挂载目录（默认 `./data`，容器内 `/data`）中的两个文件：

| 文件 | 含义 |
| --- | --- |
| `active.ndjson` | 当前正在追加的活动日志，每行一条 NDJSON |
| `active.ndjson.1` | 归档文件：设备把 `active.ndjson` **重命名**而来，不删除、不覆盖；随后新建同名 `active.ndjson` |

- 目录最多保留这两份；归档后旧文件仍可能被续写。
- 合法记录形如：`{"seq": 12, "ts": "2026-11-15T08:30:00Z", "temp": 26.315}`
  - `seq`：整数，全局按 **+1 连续递增**
  - `ts`：UTC 的 ISO-8601 字符串（必须带时区且偏移为 0，推荐以 `Z` 结尾）
  - `temp`：有限数值（拒绝 `NaN` / `Infinity` / 非数值）

## 二、采集正确性保证（核心需求）

1. **只处理上次成功后新增的字节**：采集进度持久化在 SQLite 的 `file_state` 表，
   每次从已确认的字节偏移继续读。
2. **只纳入以换行结束的合法记录**：读取后按最后一个 `\n` 切分，换行之后的
   **半行**保留在文件中、偏移不前移，下一轮再读。
3. **按 inode 跟踪进度，而非路径**：
   - 轮转后旧文件换了路径（变成 `.1`）但 inode 不变，仍从原偏移续写采集；
   - 新文件 inode 未见过，从头采集；
   - 因而**轮转与采集交错、服务重启、重复点击**都能接续，不漏记、不重记。
   - 另含防御：inode 被文件系统复用（路径角色变化）或文件被截断时从头读，
     由 `seq` 唯一约束与原文比对兜底，陈旧 inode 进度自动清理。
4. **一轮采集 = 一个事务**，任一完整行出现下列情况即**整轮拒绝**，
   已入库记录与采集进度均不变：
   - 非法 JSON / 不是对象 / 空行；
   - `seq` 非整数、`ts` 非 UTC 时刻、`temp` 非有限数值；
   - 序号未按 +1 连续递增；
   - 同一 `seq` 出现不同内容（批内或与库内冲突）。
   - 同序号且内容完全一致视为重复传输，跳过计数、不重复入库。
5. **并发保护**：进程内采集锁 + SQLite 唯一约束，重复/并发点击不会重复入库。

每次采集（含被拒绝的轮次）都在 `collect_runs` 表留痕，页面“最近采集轮次”可见
状态、完整行数、新增/重复数、半行字节数与拒绝原因。

## 三、探头跳点分析（最少剔除 + 必留/可选/必剔）

网页入口：`http://localhost:8000/analysis`。质控员设置两个参数：

- **本批起始序号**（正整数）；
- **温变上限**（℃/秒，非负有限十进制数）。

系统读取该序号起**已采集**的记录，最多 **300 条**；出现下列情况直接拒绝分析
（拒绝原因与当时水位仍留存为历史，可在历史列表查看）：

1. 该序号起没有任何已采集记录；
2. 记录数超过 300 条；
3. 时刻未按序号**严格递增**（相等或倒退）。

### 判定规则与算法

对保留点中的任意相邻两点（序号 i 在前、j 在后）要求：

```
|T_j - T_i| ≤ 温变上限 × (t_j - t_i)        （秒差）
```

把“可相邻保留”视为有向边（边只沿时间方向，构成 DAG），**剔除最少 = 保留最多**，
等价于求 DAG 最长路径。对每个点计算以其结尾/开头的最长路径长度 `f`、`g`：

- `f[i] + g[i] - 1 == L`（L 为最长路径长度）⇔ 该点出现在至少一条最优方案中；
- 按 `f-1` 把这些点分层：某层仅一个点则所有最优方案都经过它（**必留**），
  同层多个点可互相替换（**可选**）；不在任何最长路径上的点为**必剔**。

页面逐条给出三类标注、所属层，并展示**一组具体最优方案**（保留序号序列与表中
高亮行）以及每条记录的来源文件、inode 与原始行。

### 精确性（无浮点容差）

- 温度不读浮点列，而是从入库原始行 `raw` 中按 JSON number 语法切出**十进制原文**，
  用标准库 `fractions.Fraction` 精确表示；
- 温变上限同样按十进制原文转 `Fraction`；时间差换算为整数微秒；
- 所有比较都是有理数精确比较（如 `0.1 + 0.2 == 0.3` 按原文成立），
  不使用任何浮点 epsilon/容差；float 仅用于页面展示。

### 时效：参数版、采集水位与过期

每次分析都保存：

- **参数版**：`analysis_params` 中 `(start_seq, limit_text)` 唯一版本，重复使用不新增；
- **采集水位**：记录总数、最大序号、最近成功采集轮次、各文件 inode/偏移/大小快照。

过期规则：

- **采集到新记录**后，当前结论自动标记为 `stale`（过期）；
- **修改参数**后发起分析（含被拒绝的尝试），旧当前结论标记为 `stale`；
- 同参数重新分析：旧结论转为 `superseded`（被取代的历史），新结论成为当前结论；
- **分析期间若有新记录入库**：发布前复检水位发现变化，本次结论只存为
  `stale_history`，**不会成为当前结论**，需重新分析；
- 所有历史结果只追加、不改写，保留原结论与依据；分析不修改原始记录与采集进度。

分析与采集使用各自的锁，数据库开启 WAL：分析的计算在事务外进行，
**分析期间采集仍可正常完成**（有并发测试覆盖）。

## 四、目录结构

```
.
├── app/
│   ├── __init__.py        # Flask 路由：页面 / 采集 / CSV / 跳点分析 / 演示重置 / 健康检查
│   ├── collector.py       # 采集核心：增量读取、半行切分、校验、事务、进度表
│   ├── analyzer.py        # 跳点分析：Fraction 精确判定、DAG 最长路径、三分类、水位与过期
│   └── templates/
│       ├── index.html
│       ├── analysis.html        # 分析发起页 + 当前结论 + 历史列表
│       └── analysis_detail.html # 逐条标注、一组方案、依据水位
├── demo/
│   └── device_sim.py      # 设备模拟器（仅标准库）：write/half/rotate/illegal/seed/auto ...
├── tests/
│   ├── test_collector.py  # 采集器 + Web 端到端 unittest
│   └── test_analyzer.py   # 分析算法/精确性/拒绝/过期/并发（含随机暴力对照）
├── scripts/               # 本地启动 / 测试 / 演示的快捷脚本
├── data/                  # Compose 挂载的日志目录（git 仅保留 .gitkeep）
├── db/                    # SQLite 持久化目录（容器内 /instance）
├── run.py                 # 启动入口
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 五、用 Docker Compose 启动（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- 网页：`http://localhost:8000/`
- 跳点分析：`http://localhost:8000/analysis`
- CSV：`http://localhost:8000/records.csv`
- 健康检查：`http://localhost:8000/healthz`
- 卷挂载：`./data -> /data`（日志）、`./db -> /instance`（SQLite）
- 演示重置开关 `ENABLE_DEMO_RESET=1` 已在 compose 中开启，页面显示“演示重置”按钮。

另开一个终端，用模拟器对挂载目录造数据（容器与宿主机共享 `./data`）：

```bash
# 方式 A：一键完整剧本（自动调用网页的 /collect，含半行、轮转、非法拒绝、恢复）
python3 -I demo/device_sim.py --data-dir ./data auto --base-url http://localhost:8000

# 方式 B：手动逐步演示
python3 -I demo/device_sim.py --data-dir ./data seed     # 预置：.1 含半行、双文件样本
# 浏览器点“立即采集” -> 5 条入库，页面显示半行 30 字节
python3 -I demo/device_sim.py --data-dir ./data finish-half
# 再点“立即采集” -> 半行补录为 seq 6
```

> 模拟器与应用代码分离：`demo/` 是独立目录、只用标准库，可单独拷贝到宿主机运行。
> 半行辅助元数据 `.half.json` 仅模拟器使用，采集端只读 `active.ndjson*` 两个文件。

## 六、不用 Docker 的本地运行

需要 Python 3.11+ 与 Flask：

```bash
python3 -m pip install -r requirements.txt   # 或用虚拟环境
./scripts/run_local.sh                       # 默认 http://localhost:8000
# 另开终端
./scripts/demo.sh seed
./scripts/demo.sh auto
```

## 七、模拟器子命令

| 命令 | 作用 |
| --- | --- |
| `write N` | 追加 N 条完整合法记录（序号自动接续，统计时计入末尾半行占用的序号） |
| `half` | 写入一条不带换行的半行，并记录 `.half.json` 元数据 |
| `finish-half` | 在两份日志中找到半行并无损补全、加换行 |
| `rotate` | 模拟归档：`active.ndjson` 重命名为 `.1`，再新建空 `active.ndjson` |
| `illegal KIND` | 写一条完整但非法的行：`nan` / `bad-json` / `bad-ts` / `dup-conflict` |
| `seed` | 预置含半行与轮转文件的演示样本 |
| `reset` | 清空两份日志与半行元数据（数据库用网页“演示重置”清理） |
| `auto` | 一键编排 8 步完整剧本并自动调用采集接口 |

## 八、测试

```bash
# 标准库 unittest；若 Flask 在非标准 site-packages，用 TEST_SITE 指定
TEST_SITE=/path/to/site-packages ./scripts/run_tests.sh
# 或
python3 -m unittest discover -s tests -v
```

采集器覆盖：基础增量与幂等点击、半行留待下次、轮转后旧文件续写+新文件接续、
双轮转与陈旧 inode 清理、inode 复用防御、各类非法行整轮拒绝且进度不变、
序号断档/同序号异内容/同序号同内容、重启恢复、轮转与采集交错、Web 页面与 CSV。

分析器覆盖：单点跳点必剔、互斥分支可选、全保留/单点/零上限、边界相等允许、
微秒时刻、`0.1+0.2` 等原文精确判定（无浮点容差）、300/301 边界、
时刻非严格递增与空批次拒绝、参数版去重、新记录/改参数过期、同参数取代、
分析期间入库只存历史、读路径对账修复，以及 60 组随机小图的**枚举暴力对照**
（逐个点核对必留/可选/必剔与最优长度）和 Web/并发端到端流程。

## 九、数据库表

- `records`：质控记录（`seq` 唯一）、原文 `raw`、来源文件与 inode、采集时间。
- `file_state`：按 inode 的采集偏移、观测大小、当前路径、更新时间。
- `collect_runs`：每轮采集的状态（`ok`/`rejected`/`error`）与明细、错误原因。
- `analysis_params`：分析参数版（`start_seq` + `limit_text` 唯一）。
- `analysis_runs`：每次分析的状态（`ok`/`rejected`）、时效状态
  （`current`/`stale`/`stale_history`/`superseded`/`rejected`）、
  逐条结论 JSON、参数版与采集水位快照、过期/失效原因。
