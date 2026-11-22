# 命令行

安装 corlet 后会得到 `corlet` 命令（也可以用 `python -m corlet`）。

```bash
corlet [全局选项] 命令 [命令选项]
```

## 指定应用

`-a/--app`（可重复）告诉命令行应用由哪些组件组成：

| 写法 | 含义 |
|---|---|
| `-a myapp.components` | 导入模块，取其中定义的全部组件 |
| `-a myapp.components:Orders` | 模块中的某个属性：组件、组件列表 |
| `-a myapp:create_container` | 返回 `Container` 的工厂函数（若它接受 `config` 参数，会传入命令行构造的配置） |
| `-a myapp:container` | 一个现成的 `Container` 对象 |
| `-a ./components` | 目录：其中的 `*.py` 文件和包 |
| `-a app.py` | 一个 Python 文件：其中的 `Container` 对象，或者其中定义的组件 |

`Container` 目标必须是唯一的 `-a`。也可以通过环境变量 `CORLET_APP`（逗号分隔）或 `pyproject.toml`
指定，见下文。

## 全局选项

| 选项 | 说明 |
|---|---|
| `-a, --app TARGET` | 应用目标，可重复 |
| `-c, --config FILE` | 配置文件，可重复；`?` 开头表示可选 |
| `-s, --set KEY=VALUE` | 配置覆盖，如 `-s components.acme.db.port=5433`，可重复 |
| `--env-prefix PREFIX` | 读取配置的环境变量前缀，默认 `CORLET`；`''` 表示不读取 |
| `--std NAMES` | 添加标准组件，逗号分隔或 `all` |
| `--disable ID` | 禁用组件，可重复 |
| `--strict` | 启用严格模式 |
| `--entry-points / --no-entry-points` | 是否发现通过入口点安装的组件（默认开启） |
| `--log-level LEVEL` | 日志级别，见下文[日志](#日志) |
| `-v, --verbose` | 每个 `-v` 把日志级别降低一级（WARNING → INFO → DEBUG） |
| `-q, --quiet` | 只记录错误 |
| `--log-file PATH` | 把日志写入文件，而不是 stderr |
| `--log-format text\|json` | 日志格式 |

## 命令

### run

启动应用并运行，直到收到 Ctrl-C / SIGTERM，然后按序停止。退出码为 `128 + 信号编号`。

```bash
corlet -a myapp -c app.toml --std logging,health,admin run
```

### check

只装配和校验，**不启动任何组件**：依赖缺失、版本冲突、循环依赖、配置错误等。有错误时退出码为 1，
适合放进 CI。

```bash
$ corlet -a myapp check
5 component(s), 4 runnable, 3 layer(s)
  ERROR version-mismatch: [myapp.api] requires myapp.core>=2 but 1.4.0 is installed
FAILED
```

### list

```bash
$ corlet -a myapp list
ID              VERSION  STATE     ACTIVATION  PROVIDES   REASON
--------------  -------  --------  ----------  ---------  ------
myapp.db        1.0.0    resolved  eager       Database
myapp.orders    1.0.0    resolved  eager       Orders

$ corlet -a myapp list --json
```

### graph

```bash
corlet -a myapp graph                        # 文本：按层列出，并列出无法运行的组件及原因
corlet -a myapp graph --format dot | dot -Tsvg > graph.svg
corlet -a myapp graph --format mermaid       # 可直接贴进 Markdown
```

### config

```bash
corlet -a myapp -c app.toml config                   # 合并后的整棵配置树
corlet -a myapp -c app.toml config myapp.db          # 某组件的原始配置节和校验后的配置
```

### inspect

以 JSON 输出装配后的完整快照：组件、服务、扩展点、依赖层次和诊断。

### doctor

输出 Python 和 corlet 版本、各组件来源及其加载问题，再执行 `check`。

## 日志

命令行把日志写到 **stderr**（或 `--log-file` 指定的文件），命令本身的输出写到 stdout，两者互不干扰。

默认级别取决于命令：

| 命令 | 默认级别 |
|---|---|
| `run` | `INFO`：长期运行的应用，记录启动、停止等生命周期信息 |
| 其他内置命令和组件贡献的子命令 | `WARNING`：只在有问题时输出 |

级别的确定顺序：`--log-level` > `-q` / `-v` > 环境变量 `CORLET_LOG_LEVEL` > 上表的默认值。

```bash
corlet -a myapp run                          # INFO
corlet -a myapp -v import data.csv           # 子命令，INFO
corlet -a myapp --log-file app.log run       # 日志写入文件，终端保持干净
CORLET_LOG_LEVEL=DEBUG corlet -a myapp check
```

应用中加入了 [`corlet.logging`](std/logging.md#loggingcomponent) 组件时，组件启动后按它的配置
接管日志输出。

## 在 pyproject.toml 中设置默认值

```toml
[tool.corlet]
app = ["myapp.components"]
config = ["app.toml", "?local.toml"]
std = ["logging", "health"]
disabled = ["myapp.experimental"]
strict = false
```

命令行参数优先于这些默认值（`disabled` 会合并）。

## 组件贡献子命令

组件可以向 `CLI_COMMANDS` 扩展点贡献子命令。命令行**静态读取**清单中的贡献，所以子命令不用启动
应用就会出现在 `corlet --help` 中：

```python
import argparse
from corlet import Container, component, contribution
from corlet.std.cli import CLI_COMMANDS, CliCommand

async def import_orders(container: Container, args: argparse.Namespace) -> int:
    orders = container.services.instance(Orders)
    for path in args.files:
        await orders.import_file(path)
    return 0

def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("files", nargs="+")

@component(
    "shop.importer",
    extensions=[contribution(CLI_COMMANDS, CliCommand("import", "导入订单", import_orders, configure))],
)
class Importer(Component): ...
```

```bash
corlet -a shop import a.csv b.csv
```

`CliCommand(name, help, run, configure=None, needs_running=True)`：

- `run(container, args)` 可以是同步或异步函数，返回退出码（`None` 视为 0）；
- `needs_running=True` 时，命令执行前启动容器，执行后停止；为 `False` 时容器只装配不启动；
- 与内置命令或其他贡献重名的命令会被忽略并给出警告；被禁用的组件贡献的命令不会出现。
