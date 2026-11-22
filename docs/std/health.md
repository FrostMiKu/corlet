# 健康检查：`corlet.std.health`

`corlet.health` 汇总各组件贡献的健康检查和组件状态，得出整体健康状况。

```python
from corlet.std import HealthComponent
Container(..., HealthComponent)
```

## 贡献检查

```python
from corlet import component, contribution
from corlet.std import HEALTH_CHECKS, CheckResult, HealthCheck

async def db_ping() -> CheckResult:
    latency = await pool.ping()
    if latency > 1.0:
        return CheckResult.degraded("slow", latency=latency)
    return CheckResult.up(latency=latency)

@component(
    "acme.db",
    extensions=[
        contribution(HEALTH_CHECKS, HealthCheck("db", db_ping, timeout=2.0)),
        contribution(HEALTH_CHECKS, HealthCheck("replica", replica_ping, critical=False)),
    ],
)
class Db(Component): ...
```

检查函数可以是同步或异步的，返回值可以是：

| 返回值 | 解释为 |
|---|---|
| `CheckResult.up(detail, **data)` / `degraded(...)` / `down(...)` | 原样使用 |
| `True` 或 `None` | UP |
| `False` | DOWN |
| 其他值 | UP，值作为说明文字 |
| 抛出异常 / 超时 | DOWN，说明中包含异常信息 |

`HealthCheck(name, check, timeout=5.0, critical=True)`：非关键检查（`critical=False`）失败时只会
让整体变为 DEGRADED，而不是 DOWN。

## 汇总规则

```python
report = await ctx.services.instance(HealthService).report()
report.status        # Status.UP / DEGRADED / DOWN
report.checks        # 每个检查的结果：名称、所属组件、状态、说明、耗时
report.components    # {组件 id: 状态}
report.to_dict()     # 可直接序列化为 JSON
```

所有检查**并发**执行。整体状态：

- **DOWN**：任一关键检查为 DOWN；
- **DEGRADED**：任一检查为 DEGRADED、任一非关键检查失败，或有组件处于 FAILED；
- **UP**：其他情况。

## HTTP

同时加入 [`corlet.admin`](admin.md) 时，`GET /health` 返回这份报告，状态为 DOWN 时 HTTP 状态码为
503，便于接入负载均衡器和容器编排系统的探针。
