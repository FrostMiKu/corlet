# 管理端点：`corlet.std.admin`

`corlet.admin` 是一个零依赖的小型 HTTP 端点（基于 asyncio streams），用于自省、健康检查和运行期
控制。

```python
from corlet.std import AdminComponent, HealthComponent
Container(..., AdminComponent, HealthComponent)
```

## 配置

```toml
[components."corlet.admin"]
host = "127.0.0.1"
port = 8765            # 0 表示任选空闲端口
allow_control = false  # 是否开放 enable/disable/restart
token = "s3cret"       # 设置后要求 Authorization: Bearer s3cret
```

!!! warning "安全"
    默认只监听本机。暴露到网络时请务必设置 `token`，并只在需要时打开 `allow_control`。

实际监听的地址可以从 `AdminServer` 服务读取：

```python
server = container.services.instance(AdminServer)
server.port, server.url
```

## 内置路由

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 路由列表（含贡献方） |
| GET | `/health` | `HealthService` 报告；DOWN 时返回 503。未加入 `corlet.health` 时只根据组件状态给出 up / degraded |
| GET | `/components` | 所有组件的快照 |
| GET | `/components/{id}` | 单个组件 |
| GET | `/services` | 服务注册表 |
| GET | `/extensions` | 扩展点与贡献 |
| GET | `/graph?format=dot\|mermaid` | 依赖图 |
| POST | `/components/{id}/enable` | 启用（需 `allow_control`） |
| POST | `/components/{id}/disable` | 禁用（需 `allow_control`） |
| POST | `/components/{id}/restart` | 重启（需 `allow_control`） |

```bash
curl -s localhost:8765/components/shop.orders
curl -s -X POST localhost:8765/components/shop.pricing.vat/disable
# {"component": "shop.pricing.vat", "action": "disable", "state": "disabled",
#  "stopped": ["shop.pricing.vat"], "started": [], "failed": []}
curl -s "localhost:8765/graph?format=dot" | dot -Tsvg > graph.svg
```

控制操作会在容器拥有的任务中执行，所以即使操作的对象是 admin 组件自己（例如重启它），请求也能
正常返回。

## 贡献路由

其他组件可以通过 `ADMIN_ROUTES` 扩展点添加路由：

```python
from corlet import component, contribution
from corlet.std import ADMIN_ROUTES, Request, Response, Route

def stats(req: Request):
    return {"orders": STATS.count}                   # 普通数据 → JSON

async def order(req: Request):
    found = await repo.get(req.params["id"])         # 路径参数
    if found is None:
        return Response.json({"error": "not found"}, 404)
    return found

@component(
    "shop.reporting",
    extensions=[
        contribution(ADMIN_ROUTES, Route("GET", "/shop/stats", stats, "订单统计")),
        contribution(ADMIN_ROUTES, Route("GET", "/shop/orders/{id}", order)),
    ],
)
class Reporting(Component): ...
```

处理函数可以是同步或异步的，接收一个 `Request`：

| `Request` 属性 | 说明 |
|---|---|
| `method`、`path` | 请求方法与路径 |
| `params` | 路径参数，如 `{id}` |
| `query` | 查询参数（`dict[str, list[str]]`）；`req.arg("name", default)` 取第一个值 |
| `headers` | 小写化的请求头 |
| `body` / `json()` | 原始请求体 / 解析后的 JSON |

返回值：

| 返回 | 响应 |
|---|---|
| `Response(...)`、`Response.json(data, status)`、`Response.text(s, status)` | 原样 |
| `dict`、`list` 等 | 200，JSON |
| `str` | 200，纯文本 |
| `(status, body)` | 指定状态码 |
| `None` | 204 |

贡献方停止时，它的路由随之消失（之后访问返回 404）。

## 限制

这是一个运维端点，不是通用 Web 服务器：每个连接只处理一个请求（`Connection: close`），请求体上限
1 MiB，请求头上限 16 KiB。需要完整的 Web 框架时，把它（如 Starlette、aiohttp）包装成一个组件，并通过
扩展点收集路由。
