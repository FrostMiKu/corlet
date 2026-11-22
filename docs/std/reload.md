# 热重载：`corlet.std.reload`

`corlet.reload` 是一个**开发期**组件：它监视已安装组件的源文件，文件变化时重新导入模块，并用
`container.replace_component()` 原地替换其中定义的组件。被替换的组件及其依赖方会用新代码重启，
其他组件保持运行。

```python
from corlet.std import ReloadComponent
Container(..., ReloadComponent)
```

```bash
corlet -a myapp --std reload run
```

```toml
[components."corlet.reload"]
interval = 1.0                 # 轮询间隔（秒）
include = []                   # 只监视这些组件；为空时监视所有非 corlet.* 组件
exclude = ["myapp.database"]   # 永不重载
```

## 行为

- 新代码有语法错误或导入失败时，记录错误并**保留正在运行的版本**；修好后再次保存即可。
- 模块中新增的组件（id 之前不存在）不会被安装，因为组件集合在启动时就已确定；日志会提示重启应用。
- 契约按限定名作键，`ServiceRef` 是动态句柄，因此重载后的提供者对使用方透明。

## 局限

热重载基于 `importlib.reload`，有它的固有限制：其他模块中持有的旧对象（例如在模块顶层
`from x import SomeClass` 得到的类、旧类创建的实例）仍然是旧代码。把可变状态放在组件实例中、通过
服务获取依赖，而不是在模块之间直接共享对象，可以让热重载更可靠。

**不要在生产环境启用。**
