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
```

演示在临时目录内建立本地状态，结束后自动清理。测试、编译与演示在依赖已准备的环境内均不下载数据或软件。输入错误以非零退出码及错误对象报告。更多子命令可通过对应入口的 --help 查看。

容器环境可执行 `docker build -t local-analysis .`，再运行 `docker run --rm --network none local-analysis`。镜像固定基础内容摘要，构建后运行只访问本地文件。完整测试与演示也可直接使用已有 Python 环境。
