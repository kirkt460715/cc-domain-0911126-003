# 护理床位登记复核

本项目使用 Python 3.11 及以上版本，仅依赖标准库，无需安装第三方包。数据文件均为本地合成记录，不含真实个人或机构资料。

registry 保存机构及执照的时点目录；bed_events 处理事件修订、重放和隔离；indicators 计算登记定义下的床位占比和期间指标；audit 提供核查、查询、发布与恢复。

业务日和知识时间分别指定。撤销、非法前置状态和执照问题保留诊断。护理、普通、康复是登记类别；分类比例不是入住率。

在项目根目录运行完整测试：

```sh
python3 -m unittest discover -s tests -v
```

编译检查：

```sh
python3 -m compileall -q .
```

实际演示：

```sh
python3 bed_audit.py demo --fixture fixtures/registry.json
python3 bed_audit.py demo --fixture fixtures/registry.json --worksheet fixtures/disputed_units.json
```

争议证据区间以固定业务日、known_at 和指标定义为前提，以已确认床位为底数。工作表 `units` 中的每个争议单元列出若干互斥候选状态（护理、普通、康复或不在册），它们只是假设，不会写回正式事件。同一床位不在两个单元重复计数；各机构候选总量受当日有效执照上限约束；单元之间可用 `excludes`、`requires`、`same_case` 关系。候选身份与已确认床位、机构或区域不相容，或已确认底数本身超执照时，报告 `evidence_conflict` 而不自动扩大容量。

每个区域的护理占比与达标所需转化数返回完整枚举得到的可达最小、最大值，并为两端返回可重放的具体候选组合（`witnesses`）。`outer_bounds` 中的独立分子/分母松弛只作外边界参考，不作为精确界。零分母状态单列为 `zero_denominator`，`ratio` 保持 `None` 而非 0。结论分一定达标、一定不达标、证据不足三种；父区域的极值在全局枚举中与子区域联合求解。达到资源限额时 `status` 为 `resource_limit_exceeded`，并以 `extrema_proven`、`conclusion_proven` 区分已证界与仍未证明的结果。

核查人员固定某个争议单元的候选后，可与保留的基线报告比较区间收窄程度：

```sh
python3 bed_audit.py dispute-interval --fixture fixtures/registry.json --worksheet fixtures/disputed_units.json --day 2026-11-10 --known-at 2026-11-12T00:00:00Z
python3 bed_audit.py dispute-narrowing --fixture fixtures/registry.json --worksheet fixtures/disputed_units.json --baseline baseline.json --day 2026-11-10 --known-at 2026-11-12T00:00:00Z --pin '{"case-a":"a-nursing"}'
```

HTTP 服务对应 `/disputes/summary`、`/disputes/interval` 与 `/disputes/narrowing`；`report` 传入工作表时会附带 `dispute_summary` 与 `dispute_interval`。导出证据包时额外写入 `dispute-worksheet.json`、`dispute-summary.json`、`dispute-interval.json`，并同样校验摘要。

演示在临时目录内建立本地状态，结束后自动清理。测试、编译与演示在依赖已准备的环境内均不下载数据或软件。输入错误以非零退出码及错误对象报告。更多子命令可通过对应入口的 --help 查看。

容器环境可执行 `docker build -t local-analysis .`，再运行 `docker run --rm --network none local-analysis`。镜像固定基础内容摘要，构建后运行只访问本地文件。完整测试与演示也可直接使用已有 Python 环境。
