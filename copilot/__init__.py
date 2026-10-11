"""桌面副驾（desktop copilot）：通过 ACP 驱动 DSH 的适配层。

本包是可选功能：未安装 agent-client-protocol 或 copilot.json enabled=false
时，数据服务/搜索面板不得导入本包的 ACP 相关模块（见 copilot.config 的
懒加载约定）。SQLite 业务记录与既有 usage log 分离。
"""

__version__ = "0.1.0"

# 官方 ACP Python SDK 的锁定版本（01 票实测通过后写入 requirements-copilot.txt）
ACP_SDK_REQUIREMENT = "agent-client-protocol==0.12.1"
