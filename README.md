# schemagen

从 JSON 样本推断 schema，一次生成 **JSON Schema / TypeScript / Pydantic** 三种草稿。

给后端/数据同学用的小工具 —— 拿到一份 API 返回或日志样例，懒得手写类型定义时用。
Python 标准库零依赖，单个文件拎走即用；推断全在本地跑，不上传任何数据。

## 安装

零依赖，Python 3.10+ 即可：

```bash
python3 -m schemagen --help
# 或
python3 schemagen.py examples/user.json --name User
```

## 快速开始

```bash
# 三种输出一次全给（默认）
python3 -m schemagen examples/user.json --name User

# 只要其中一种
python3 -m schemagen examples/user.json --ts
python3 -m schemagen examples/user.json --pydantic

# 写文件：schema.json / types.ts / models.py
python3 -m schemagen examples/events.jsonl --name Event -o ./out

# 所有字段标必填（默认：只在全部样本都出现的字段必填）
python3 -m schemagen sample.json --strict
```

输入支持两种：`.json`（单个对象，或数组——数组每个元素算一个样本）、
`.jsonl`（每行一个样本，自动合并）。

## 推断规则

| 情况 | 处理 |
|---|---|
| 字段在全部样本都出现 | 必填（`required` / 无 `?` / 无默认值） |
| 某样本缺字段 | 可选（`?` / `Optional[...] = None`） |
| 值为 `null` | 可空（`type: ["string","null"]` / `\| null` / `Optional`）；注意"缺字段"和"值为 null"是两回事 |
| 恒为 `null` 的字段 | 类型未知 → Pydantic 生成 `Any = None` 并打 TODO，TS 生成 `null` |
| ISO 8601 字符串 | JSON Schema 加 `format: date-time` |
| 字符串值 2–8 种**且有值重复出现** | 判为 enum（`Literal[...]` / 字面量联合）。只出现一次的 ID 类字符串（如订单号）不会被锁成 enum |
| 数组元素类型不一致 | `anyOf` / 联合类型；元素全未知时退化为 `any`/`unknown` |
| 嵌套对象 | 递归生成 `UserAddress`、`UserOrdersItem` 这样的命名类型 |

`bool` 先于 `int` 判断（Python 里 `bool` 是 `int` 子类，不先判会把 `true` 推成整数）。

## 诚实说明（先看这段再用）

- **输出是草稿，不是定稿**：样本推断本质是猜。样本越多越准，只有一个样本时所有字段都会被标"必填"——这通常是错的，请人工过一遍。
- **enum 是启发式**：靠"值重复出现"判断封闭标签集，业务上开放的字段也可能误判；反过来样本太少也会漏判。
- **TS 输出未经 tsc 编译验证**（本机无 tsc），只做了人工 eyeball 检查；Pydantic 输出经过 `py_compile` + 真实 pydantic 2.x 构造/校验测试。
- 类型名来自 `--name` 和字段名的 PascalCase 拼接，重名会自动加数字后缀。
- 非流式、无网络：整个过程不离开本机。

## 示例

`examples/user.json`（嵌套对象、日期时间、null、对象数组）与
`examples/events.jsonl`（4 行埋点日志：`element` 只在部分行出现 → 可选；
`event` 有重复值 → enum）可直接拿来试。

## License

MIT，Copyright (c) 2026 ljiang9。
