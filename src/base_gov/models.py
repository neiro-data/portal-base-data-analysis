"""Typed records returned by the BASE portal search endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Entity:
    """A contracting/contracted entity as listed under `Entidades`."""

    id: int
    nif: str
    description: str
    country: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Entity:
        return cls(
            id=int(payload["id"]),
            nif=str(payload.get("nif", "")).strip(),
            description=str(payload.get("description", "")).strip(),
            country=str(payload.get("country", "")).strip(),
        )


@dataclass(frozen=True, slots=True)
class Contract:
    """A contract row as listed under `Contratos`.

    The portal returns a narrow projection in search results; the full record
    lives on the detail page (`/Base4/pt/detalhe/?type=contratos&id=...`).
    """

    id: int
    object_description: str
    procedure_type: str
    contracting: str
    contracted: str
    initial_price: str
    publication_date: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Contract:
        return cls(
            id=int(payload["id"]),
            object_description=str(payload.get("objectBriefDescription", "")).strip(),
            procedure_type=str(payload.get("contractingProcedureType", "")).strip(),
            contracting=str(payload.get("contracting", "")).strip(),
            contracted=str(payload.get("contracted", "")).strip(),
            initial_price=str(payload.get("initialContractualPrice", "")).strip(),
            publication_date=str(payload.get("publicationDate", "")).strip(),
        )


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of results plus the server-reported total for the whole query."""

    total: int
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.items
