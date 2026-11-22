# 扩展点

服务是"我提供一个对象给你用"；扩展点是"**我定义一个槽，大家往里贡献**"。定义方决定怎样
调用这些贡献，贡献方决定自己排在什么位置。

典型用途：CLI 子命令、HTTP 路由、中间件链、序列化器、校验规则、健康检查、定时任务。

## 定义扩展点

```python
from corlet import ExtensionPoint, Strategy

PRICING_RULES = ExtensionPoint(
    "shop.pricing.rules",         # 全局唯一 id
    contract=PricingRule,         # 可选：贡献必须满足的类 / Protocol
    strategy=Strategy.PIPELINE,   # 调用策略
    method="apply",               # 可选：调用贡献的哪个方法（不设则调用贡献本身）
    description="调整订单金额",
    blocking=False,               # True：同步贡献在线程中执行
)

@component("shop.orders", defines=[PRICING_RULES])
class OrdersComponent(Component): ...
```

扩展点由清单的 `defines` 声明，在装配时就已定义（不必等定义方启动）。两个不同的组件定义同一个
id 会报错。

## 调用策略

| 策略 | `invoke()` 的返回值 | 用途 |
|---|---|---|
| `BROADCAST` | `None`，全部调用 | 通知类钩子 |
| `COLLECT`（默认） | 所有返回值组成的列表 | 收集贡献（路由、命令） |
| `FIRST` | 第一个非 `None` 的返回值 | 责任链、按优先级查找处理者 |
| `PIPELINE` | 依次传递：上一个的输出是下一个的输入 | 中间件、过滤器、金额计算 |

```python
total = await ctx.extensions.invoke(PRICING_RULES, subtotal, customer="bob")
# PIPELINE：第一个位置参数是初始值，其余参数原样传给每个贡献

handler = await ctx.extensions.invoke(SERIALIZERS, "application/json")   # FIRST
results = ctx.extensions.invoke_sync(VALIDATORS, payload)                # 同步版本
```

`invoke()` 同时支持同步和异步的贡献；`invoke_sync()` 只能调用同步贡献，遇到协程会抛
`ExtensionContractError`。

默认情况下，任何贡献抛出的异常都会向上传播。传 `errors="log"` 则记录日志并跳过失败的贡献：

```python
await ctx.extensions.invoke(HOOKS, event, errors="log")
```

不想调用、只想拿到贡献本身时：

```python
ctx.extensions.implementations(ADMIN_ROUTES)   # 贡献的对象，按调用顺序
ctx.extensions.extensions(ADMIN_ROUTES)        # Extension 记录（含 owner、name、metadata）
```

## 贡献

声明式（推荐）：贡献随组件进入 ACTIVE 时出现，停止时撤销。

```python
from corlet import contribution

@component(
    "shop.pricing.vat",
    extensions=[contribution(PRICING_RULES, Vat(), name="vat", metadata={"rate": 0.2})],
)
class VatComponent(Component):
    async def start(self, ctx): pass
```

命令式：在 `start()` 中贡献，适合依赖配置或其他服务构造的贡献。

```python
@component("shop.pricing.discount", config=DiscountConfig)
class DiscountComponent(Component):
    async def start(self, ctx, config: DiscountConfig):
        ctx.extensions.contribute(PRICING_RULES, Discount(config), name="discount", before=["vat"])
```

如果扩展点设置了 `contract` 或 `method`，不满足的贡献会在贡献时被拒绝（`ExtensionContractError`）。

## 顺序

贡献的顺序由三条规则决定，结果是确定的：

1. `before=[...]` / `after=[...]` 约束：可以引用其他贡献的 `name`，也可以引用贡献方的组件 id；
2. 在满足约束的前提下，`order` 小的先执行；
3. 再按贡献的先后顺序。

```python
contribution(POINT, a, name="auth", order=-10)          # 尽量靠前
contribution(POINT, b, name="cors", before=["auth"])    # 必须在 auth 之前
contribution(POINT, c, after=["acme.logging"])          # 在 acme.logging 组件的所有贡献之后
```

约束形成环时，记录一条警告，相关贡献退回到按 `order` 排序。

## 跟踪贡献

定义方常常需要在贡献出现或消失时做出反应，例如 `corlet.scheduler` 为每个贡献的任务启停计时器：

```python
def added(ext):   schedule(ext.impl, owner=ext.owner)
def removed(ext): unschedule(ext.impl)

ctx.extensions.watch(JOBS, added, removed)   # 默认先对已有贡献回放 added
```

## 扩展点与事件的区别

| | 扩展点 | 事件 |
|---|---|---|
| 关系 | 契约调用：定义方需要贡献的结果 | 通知：发布方不关心谁在听 |
| 返回值 | 按策略汇总 | 无 |
| 顺序 | 确定，可约束 | 处理器并发执行 |
| 失败 | 默认向调用方传播 | 相互隔离，只记录日志 |
