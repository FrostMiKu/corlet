# 调度：`corlet.std.scheduler`

`corlet.scheduler` 组件运行周期任务和延迟任务。任务**属于创建它的组件**：所属组件停止时，任务
自动取消。

```python
from corlet.std import SchedulerComponent
Container(..., SchedulerComponent)
```

## 命令式：Scheduler 服务

```python
from corlet.std import Scheduler

@component("acme.cache")
class Cache(Component):
    async def start(self, ctx, scheduler: Scheduler):
        scheduler.every(60, self.evict, name="evict")              # 每 60 秒
        scheduler.every(5, self.refresh, run_now=True, jitter=1)   # 立即执行一次，之后每 5 秒（加随机抖动）
        scheduler.after(10, self.warm_up)                          # 10 秒后执行一次
```

`every()` 的参数：

| 参数 | 说明 |
|---|---|
| `interval` | 两次运行**开始**之间的间隔（秒） |
| `func` | 无参数的同步或异步函数 |
| `name` | 显示名称，默认取函数名 |
| `initial_delay` | 第一次运行前的等待，默认等于 `interval` |
| `jitter` | 每次运行前额外随机等待 0 ~ `jitter` 秒 |
| `run_now` | 立即运行一次，然后按间隔运行 |

行为：

- 同一个任务的运行**不会重叠**：如果一次运行超过了间隔，下一次在它结束后立即开始。
- 任务抛出的异常被记录，计入 `failures`，任务继续按计划运行。
- 任务在所属组件的上下文中运行，日志带有该组件的 id。

调度器是可选依赖时，这样写：

```python
async def start(self, ctx, scheduler: Scheduler | None = None):
    if scheduler is not None:
        scheduler.every(30, self.report)
```

## 声明式：贡献到 JOBS

```python
from corlet import contribution
from corlet.std import JOBS, Job

@component(
    "acme.report",
    extensions=[contribution(JOBS, Job("daily-report", 86400, send_report, run_on_start=False))],
)
class Report(Component):
    async def start(self, ctx): pass
```

`Job(name, interval, func, initial_delay=None, jitter=0.0, run_on_start=False)`。贡献随组件激活
出现、随组件停止撤销，调度器会相应地启动或取消任务。

## 查看与取消

```python
job = scheduler.every(60, work)
job.active, job.runs, job.failures, job.last_run, job.last_error
job.cancel()

for j in scheduler.jobs():          # 当前所有任务，按 owner 和名称排序
    print(j.owner, j.name, j.runs)
```
