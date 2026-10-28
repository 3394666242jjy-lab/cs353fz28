from typing import Literal

from app.schemas.common import ContractModel


class Liveness(ContractModel):
    status: Literal["ok"] = "ok"
    service: str = "careerpilot-api"
    version: str = "0.1.0"


class Readiness(ContractModel):
    status: Literal["ready", "not_ready"]
    dependencies: dict[str, Literal["ok", "unavailable"]]


class Capabilities(ContractModel):
    service: str = "CareerPilot"
    mode: Literal["scaffold"] = "scaffold"
    version: str = "0.1.0"
    features: dict[str, bool] = {
        "authentication": True,
        "document_upload": True,
        "job_import": True,
        "rag": False,
        "agent_analysis": False,
    }
