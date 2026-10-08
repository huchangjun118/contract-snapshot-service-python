"""服务入口：配置来自环境与配置文件，默认落盘、跨重启保留数据。"""
import uvicorn

from contract_snapshot.api import create_app
from contract_snapshot.config import load_config
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore

config = load_config()
store = SnapshotStore(config.resolved_db_path())
service = ContractService(store, config)
app = create_app(service)


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
