# 开源桌面软件中文译句编校与语言包工作台

共用中文译句的多个桌面发行版，不能只按英文原句或字符串相似度复用。这个项目用 Python 3.11 标准库实现：

- 按 **动作 + 对象 + 界面场景** 匹配译句，区分筛选器上的“清除筛选”和最近记录旁的“清空记录”；
- 定位同一原文的多个用法、拆句/合句 lineage，允许译者按具体用法复核或禁止复用；
- 解析并渲染资料中的 ICU 子集，按实际可达的 `plural` / `select` / 嵌套分支校参数、单位和语境；
- 术语按发行版和 `usage` 场景选择，不做全局一词一译；
- 句意核准（`meaning`）和目标目录可用性核准（`display`）分开保存；
- 参数改名、usage 变更、拆合句后，用核准依据哈希判断能否继承；
- 按发行版冻结契约选包，只走该场景/版本允许的 `exact-approved → usage-approved → approved-same-context → source` 层级；
- 生成键到字符串的 UTF-8 JSON 语言包、来源清单和 SHA-256，可重复构建、可验包。

## 快速开始

```sh
# 原有资料字段检查
python3 read_materials.py

# 门禁：会列出 t4/t5/t6 的参数、单位和渲染上下文问题
python3 workbench.py check

# 查看同句多用和每个具体用法的候选
python3 workbench.py reuse

# 查看参数改名、usage 变更、拆句/合句的核准继承
python3 workbench.py impact
python3 workbench.py preview-change c2

# 查询某发行版在具体场景中的术语（main 用“项”，1.8 用“个项目”）
python3 workbench.py glossary --release main --usage '同步状态/队列' --source 'One item'
python3 workbench.py glossary --release 1.8 --usage '同步状态/队列' --source '# items'

# 生成两个发行版的 zh-Hans 语言包和可核对清单
python3 workbench.py build --release main
python3 workbench.py build --release 1.8
# 或一次重建资料中的全部发行版：
python3 workbench.py build-all

# 运行全部测试
python3 -m unittest discover -s tests -v
```

默认产物：

```text
dist/<release>/zh-Hans/desktop.json
dist/<release>/zh-Hans/desktop.manifest.json
```

`desktop.json` 始终是客户端契约要求的“键 → ICU 文本消息”对象；来源、核准、依据哈希、渲染样例和拒绝原因放在旁边的 manifest 中。

## 志愿者持续编校流程

下面命令产生的状态默认写入 `state/workbench-state.json`，可提交到仓库让团队共享。

### 1. 查看失败的具体使用位置

```sh
python3 workbench.py check --release 1.8
```

输出会包含发行版、键、界面路径、相邻提示、ICU 分支、触发参数和实际渲染。例如 `t4` 会用 `count=3, secs=42` 显示：

```text
失败位置: plural[other]
触发参数: {"secs": 42, "count": 3}
实际渲染: 还有 3 个项目
```

译者可直接看出这条弹窗里没有剩余秒数，也没有“秒”这个单位。

### 2. 提交修正译句

```sh
python3 workbench.py submit \
  --release 1.8 --key sync.remaining \
  --message '{count, plural, other {剩余 # 个项目（{secs} 秒）}}' \
  --reviewer volunteer-a
```

新草稿两种核准都是 `pending`，不会自动进包。

### 3. 维护者分别核准句意和目录可用性

```sh
python3 workbench.py approve draft-xxxxxxxx meaning \
  --reviewer maintainer-a --note "数量与等待时间语义正确"

python3 workbench.py approve draft-xxxxxxxx display \
  --reviewer maintainer-b --note "r3 契约和 zh-Hans 渲染通过"
```

只核准 `meaning` 时，门禁仍会报 `display_not_approved`。

### 4. 按具体用法允许或禁止复用

禁止把 `t1`（清除筛选）复用到 `1.8/nav.reset`：

```sh
python3 workbench.py decide-reuse \
  --target-release 1.8 --target-key nav.reset \
  --candidate-id t1 --decision forbidden
```

如果某句已经由译者针对某个用法复核，可把 `--decision approved`。

若要在所有其他用法中禁止搬运某条译句，但保留它原键上的直接核准：

```sh
python3 workbench.py decide-reuse \
  --target-release '*' --target-key '*' \
  --candidate-id t2 --decision forbidden --scope translation
```

具体用法决定优先于全局决定。

### 5. 重新出包并验包

```sh
python3 workbench.py build --release 1.8
python3 workbench.py verify dist/1.8/zh-Hans/desktop.manifest.json
```

同一发行版、语言和 catalog 名称会覆盖生成同一组成品；manifest 中的 `pack_sha256` 和依据哈希可用于核对。

## 当前合成资料对应的关键结果

- `main/search.clear` 与 `1.8/nav.reset` 属于“筛选工具栏”场景，选择/建议“清除筛选”（`t1`）。
- `main/history.clear` 与 `1.8/history.reset` 属于“最近记录”场景，选择“清空记录”（`t2`）。
- `main` 的 `sync.remaining` 契约是 `files/size`，使用 `t3`。
- `1.8` 冻结在 `r3`，契约是 `count/secs`，只使用本版已核准的 `t7`；不会把主线 `t3` 当作“看起来相同”的译句搬过来。
- `t4` 漏 `secs/秒`、`t5` 的 `saved` 分支漏 `name`、`t6` 漏 `hotkey`，都会在对应渲染上下文上报错且不能作为已核准译文入包。
- `c1` 是 `secs → wait` 改名：参数语义/单位未变时句意核准可继承，但精确参数名变化使目录可用性核准必须重做。
- `c2` 把 `search.clear` 的 usage 从筛选工具栏改到隐私/最近记录：旧的“清除筛选”句意依据失效；变动预览会改选同场景的 `t2`。
- `c3` 是 `search.active`、`search.empty` 合并为 `search.status` select；`impact` 会定位旧键、新键、修订号和建议复核分支 `select[saved]` / `select[other]`。

## Python API

```python
from i18n_workbench import Workbench

wb = Workbench("/path/to/project")

gate = wb.gate()                 # 实际渲染门禁
reuse = wb.reuse_report()        # 复用簇与每个用法的候选
impact = wb.change_impact()      # changes 的核准继承分析
preview = wb.preview_change_selection("c2")

pack = wb.build_pack(release="1.8", locale="zh-Hans", catalog="desktop")
ok = wb.verify_pack(pack["manifest_path"])
```

临时工作流状态可注入 `WorkbenchState.open("/tmp/state.json")`，避免污染仓库中的 `state/workbench-state.json`。

## 设计约束

- 只使用 Python 3.11 标准库。
- 不修改 `data/*.jsonl` 中的原始投稿；志愿者动作写入独立状态文件。
- ICU 支持范围以 `data/icu_subset.jsonl` 为准：命名变量、`plural`（`one/other/=0` 等）、`select`、`#` 和嵌套分支。
- 选择语言包时同时要求：句意核准、目录可用性核准、渲染门禁无 error、目标契约兼容、未被该用法禁止。
- 术语表的 `permit_fallback` 是场景级授权；例如“最近记录”没有授权 `source` 时，禁止同场景译句后该键会进入 manifest 的 `unresolved`，而不是硬塞英文或错误中文。
