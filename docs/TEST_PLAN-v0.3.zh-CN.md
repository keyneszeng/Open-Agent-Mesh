# v0.3 多轮自动化验证计划

| 轮次 | 验证目标 | 自动化命令 | 通过条件 |
|---|---|---|---|
| 1 | v0.2 全量回归 + v0.3 单元测试 | `python -m pytest -q` | 全部通过 |
| 2 | 数据库租约/迁移/并发、权限/审批 | `python -m pytest tests/test_reliability_v03.py` | 全部通过 |
| 3 | 确定性随机 DAG 与 RPC 边界、无 5xx | `python -m pytest tests/test_fuzz_v03.py` | 全部通过 |
| 4 | 双进程 A2A、独立 HTTP Agent、Node/Skill | `python tests/smoke_network.py` | 所有检查通过 |
| 5 | 运行中 SIGKILL、重启、租约到期恢复 | `python tests/chaos_network.py` | 尝试次数为 2、事件顺序正确 |
| 6 | HTTP 并发、幂等、DAG fan-out | `python tests/stress_http.py` | 100 个任务、10 工作流最终均完成 |
| 7 | 跨 Python 3.10/3.12/3.13 | GitHub Actions | 三个平台全部绿灯 |
| 8 | 资源警告/语法 | `python -W error::ResourceWarning -m pytest -q`, `python -m compileall -q src examples` | 无警告、无语法错误 |

请查看 `docs/RELIABILITY-v0.3.zh-CN.md` 获取审计、威胁模型和仍未覆盖的场景；测试中全部身份和服务均使用本机演示数据，勿用于生产。
