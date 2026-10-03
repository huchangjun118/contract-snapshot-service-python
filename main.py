import os

import uvicorn

from contract_snapshot.api import create_app
from contract_snapshot.config import CaptureConfig
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore

store = SnapshotStore(os.environ.get("CONTRACT_SNAPSHOT_DB", ":memory:"))
service = ContractService(store, CaptureConfig())
app = create_app(service)


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
