"""服务入口。

配置来源与优先级（详见 docs/contract-snapshots.md）：
环境变量 CONTRACT_SNAPSHOT_* > 配置文件（CONTRACT_SNAPSHOT_CONFIG）> 默认值。

存储默认落盘到 ./.contract_snapshot/contracts.db（重启后仍在），
可用环境变量 CONTRACT_SNAPSHOT_DB 覆盖；``:memory:`` 表示仅内存。
"""
import os

import uvicorn

from contract_snapshot.api import create_app
from contract_snapshot.config import load_config
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore

# 存储位置：显式环境变量优先；否则 db_path=None 由存储层解析默认落盘位置。
store = SnapshotStore(os.environ.get("CONTRACT_SNAPSHOT_DB"))
config = load_config()
service = ContractService(store, config)
app = create_app(service)


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
