import uvicorn
from fastapi import FastAPI

from contract_snapshot.api import router as contracts_router

app = FastAPI(title="Contract Snapshot Service")

app.include_router(contracts_router)


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
