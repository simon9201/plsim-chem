from sqlalchemy import select

from Data.Model.model import Base, Plant, ScenarioRow
from Modules.Scenario.scenario import Scenario

# Saving and loading scenarios (Modules/Scenario) in the plant database.


def _plant(session, plant_name):
    Base.metadata.create_all(session.get_bind())        # older databases do not have the scenario table yet
    return session.scalars(select(Plant).where(Plant.name == plant_name)).one()


def save_scenario(session, plant_name, scenario, overwrite=True):
    plant = _plant(session, plant_name)
    row = next((r for r in plant.scenarios if r.name == scenario.name), None)
    if row is not None and not overwrite:
        raise ValueError(f"Scenario '{scenario.name}' already exists")
    data = scenario.to_dict()
    if row is None:
        row = ScenarioRow(name=scenario.name)
        plant.scenarios.append(row)
    row.description, row.events = data["description"], data["events"]
    session.commit()


def load_scenario(session, plant_name, name):
    row = next((r for r in _plant(session, plant_name).scenarios if r.name == name), None)
    if row is None:
        raise ValueError(f"No scenario '{name}'")
    return Scenario.from_dict({"name": row.name, "description": row.description, "events": row.events})


def list_scenarios(session, plant_name):
    return [r.name for r in _plant(session, plant_name).scenarios]


def delete_scenario(session, plant_name, name):
    plant = _plant(session, plant_name)
    row = next((r for r in plant.scenarios if r.name == name), None)
    if row is not None:
        plant.scenarios.remove(row)
        session.commit()
