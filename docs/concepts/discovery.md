# 发现与打包

容器从一组**组件来源 (`ComponentSource`)** 获取组件类。来源可以组合使用。

## 传给容器的东西

```python
Container(
    OrdersComponent,                     # 组件类
    myapp.components.billing,            # 模块：取其中定义的全部组件
    "myapp.components.audit",            # 模块路径字符串：同上，按需导入
    "myapp.extra:SpecialComponent",      # 模块:属性
    [A, B, C],                           # 序列
    DirectorySource("./plugins"),        # 任意 ComponentSource
    entry_points=True,                   # 再加上已安装发行包的入口点
)
container.add(MoreComponent)             # 启动前还可以继续添加
```

## 内置来源

| 来源 | 说明 | 失败处理 |
|---|---|---|
| `ExplicitSource(*items)` | 直接给出的类、模块、序列，或返回这些东西的无参工厂函数 | — |
| `ModuleSource(*names, recursive=False)` | 导入模块并收集组件；`"pkg.mod:Name"` 选择单个属性；`recursive=True` 时扫描子模块 | 默认严格：导入失败直接抛出 |
| `EntryPointSource(group="corlet.components")` | 已安装发行包通过入口点声明的组件 | 默认宽松：记录问题并跳过 |
| `DirectorySource(path)` | 目录中的 `*.py` 文件和包（跳过 `_` 和 `.` 开头的） | 默认宽松 |

宽松来源把加载失败记录在 `source.issues` 中，`corlet doctor` 会把它们列出来。所有来源都接受
`strict=` 参数来切换行为。

`DirectorySource` 用一个基于路径的合成包名导入模块，所以两个目录中的同名文件不会冲突。

## 发布可安装的组件包

在组件包的 `pyproject.toml` 中声明入口点：

```toml
[project.entry-points."corlet.components"]
auth = "acme_auth:AuthComponent"     # 一个组件类
billing = "acme_billing.components"  # 一个模块：其中定义的全部组件
```

应用侧打开入口点发现：

```python
Container(..., entry_points=True)
```

`corlet` 命令行默认开启入口点发现（`--no-entry-points` 关闭）。这样 `pip install acme-auth`
就完成了组件安装；用 `container.disabled` 配置可以在不卸载的情况下关闭它。

### 组件包的建议结构

```
acme_auth/
├── __init__.py
├── contracts.py     # 对外暴露的契约（Protocol），其他包依赖这里
├── component.py     # @component 类
└── impl.py          # 实现
```

- 把契约放在独立的模块（甚至独立的发行包）中。使用方只依赖契约，不依赖实现。
- 组件 id 使用包名作前缀，如 `acme.auth`。
- 用 `version` 和 `requires=["acme.core>=1.2"]` 表达组件之间的兼容性要求。

## 组件 id 冲突

两个**不同**的类声明了同一个 id 时，`assemble()` 抛出 `DuplicateComponentError`，并给出两个类的
完整路径。同一个类被多个来源重复发现则没有问题。
