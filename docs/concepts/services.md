# 服务

服务注册表是组件之间交换能力的地方：提供者以**契约**为键注册对象，使用者按契约查找。

## 契约

契约是查找服务用的键，可以是：

- `typing.Protocol`（推荐）。加 `@runtime_checkable` 后，注册时会做结构检查；
- 抽象基类或普通类，注册时用 `isinstance` 检查；
- 字符串，例如 `"db.url"`，不做任何检查。

契约按**限定名**（`模块.类名`）作键，所以热重载后重新创建的 Protocol 类仍然对应同一个槽位。

## 注册

```python
reg = ctx.services.register(
    Database,
    PgPool(dsn),
    properties={"region": "eu", "readonly": False},  # 任意元数据，可用于过滤
    ranking=10,                                       # 越高越优先；相同时先注册者优先
)
reg.unregister()   # 一般不需要：组件停止时自动撤销
```

应用代码（不属于任何组件）可以注册**外部服务**，它们同样能满足 `requires_services`：

```python
container.register_service(Clock, SystemClock())   # 最好在 start() 之前注册
```

如果在容器启动之后才注册，调用 `await container.reconcile()`，让此前因缺少该服务而无法运行的
组件启动。

## 查找

```python
db  = ctx.services.get(Database)                 # 动态 ServiceRef；没有则抛 ServiceNotFound
db  = ctx.services.find(Database)                # 同上，没有则返回 None
all = ctx.services.get_all(Database)             # 绑定型 ServiceRef 列表，最优在前
eu  = ctx.services.get(Database, {"region": "eu"})            # 按属性过滤
rw  = ctx.services.get_all(Database, lambda p: not p["readonly"])  # 按谓词过滤
raw = ctx.services.instance(Database)            # 直接拿到对象
ref = await ctx.services.acquire(Database)       # 必要时激活 lazy 提供者
ctx.services.has(Database)                       # 是否存在
```

`ServiceNotFound` 的错误信息会说明为什么找不到，例如
`no service registered for Database; declared by acme.db (disabled)`。

## ServiceRef：动态句柄与绑定句柄

查找返回的是 `ServiceRef` 句柄而不是裸对象。句柄会把属性访问转发给服务，所以 `db.query(...)`
可以直接调用。

| | 动态句柄 | 绑定句柄 |
|---|---|---|
| 来源 | `get()`、`find()`、`ref()`、注入 `ServiceRef[T]` | `get_all()`、`watch()` 回调 |
| 指向 | 每次访问时重新选择最优的匹配注册 | 固定指向某一次注册 |
| 提供者离开后 | 自动切换到下一个；都没有时抛 `ServiceUnavailable` | 抛 `ServiceUnavailable` |
| 适用场景 | 长期持有、提供者可能替换 | 需要逐个处理所有提供者 |

```python
ref = ctx.services.get(Database)
ref.available        # 当前是否有提供者
ref.resolve()        # 取出服务对象本身
ref.provider         # 当前提供者的组件 id
ref.properties       # 当前注册的属性
ref.registration     # 当前的 ServiceRegistration
```

!!! note "名称冲突"
    `resolve`、`available`、`provider`、`properties`、`registration`、`contract`、`dynamic`
    这几个名字属于句柄本身。如果服务对象有同名属性，请用 `ref.resolve().<name>` 访问。

## 跟踪服务的出现和消失

```python
def added(ref):    print("new database from", ref.provider)
def removed(ref):  print("database gone:", ref.provider)

ctx.services.watch(Database, added, removed)              # 默认先对已有服务回放 added
ctx.services.watch(Database, added, flt={"region": "eu"}, replay=False)
```

- 回调可以是同步函数，也可以是协程函数（会被调度为任务）。
- `removed` 回调执行时，服务还没有真正撤销，回调里仍然可以访问它。
- watch 记在调用组件名下，组件停止时自动取消。

## 服务与依赖解析

- `requires_services=[Database]`（或注入 `db: Database`）让组件排在所有 `Database` 提供者之后
  启动，并要求至少有一个提供者可用。
- 提供者停止时，必需该契约的组件会被重启或停止，所以注入的裸对象不会悬挂。
- 可选依赖只影响启动顺序；需要应对提供者来来去去时，使用 `ServiceRef` 或 `watch`。

## 严格模式

`Container(strict=True)` 下：

- 组件只能查找 `requires_services` 中声明过的契约（以及自己 `provides` 的契约）；
- 只能注册 `provides` 中声明过的契约。

违反时抛出 `PermissionDenied`。严格模式用来保证清单如实反映依赖，便于审查架构。
