from sqlalchemy import select

from Data.Model.model import ChemComponent, ChemReaction
from Modules.Flow.chemistry import (DEFAULT_COMPONENTS, DEFAULT_REACTIONS, Component, Reaction,
                                    set_chemistry, use_default_chemistry)

# =====================================================================
# The chemistry library lives in the database (tables component / reaction)
# so a scenario can add components and reactions without touching code.
# =====================================================================


THERMO_KEYS = ("freezing_point_c", "antoine_a", "antoine_b", "antoine_c", "heat_of_vaporization", "cp_gas")


def seed_chemistry(session):
    """Insert the built-in components/reactions that are not in the database yet. Existing rows are left alone."""
    have = set(session.scalars(select(ChemComponent.name)))
    for c in DEFAULT_COMPONENTS:
        if c.name not in have:
            session.add(ChemComponent(name=c.name, molar_mass=c.molar_mass, density=c.density, cp=c.cp,
                                      enthalpy_formation=c.enthalpy_formation, attributes=dict(c.attributes)))
    have = set(session.scalars(select(ChemReaction.name)))
    for r in DEFAULT_REACTIONS:
        if r.name not in have:
            session.add(ChemReaction(name=r.name, stoichiometry=dict(r.stoichiometry), k_ref=r.k_ref,
                                     activation_energy=r.activation_energy, orders=dict(r.orders),
                                     heat_of_reaction=r.heat_of_reaction, description=r.description))
    # Databases created before the thermo data existed: add the missing thermo keys to the built-in rows
    # (never overwrites a value you changed).
    for c in DEFAULT_COMPONENTS:
        row = session.scalars(select(ChemComponent).where(ChemComponent.name == c.name)).one_or_none()
        if row is None:
            continue
        missing = {k: v for k, v in c.attributes.items() if k in THERMO_KEYS and k not in (row.attributes or {})}
        if missing:
            row.attributes = {**(row.attributes or {}), **missing}
    session.commit()


def load_chemistry(session):
    """Make the database the active chemistry library. An empty database keeps the built-in defaults."""
    comps = [Component(c.name, c.molar_mass, c.density, c.cp, c.enthalpy_formation, dict(c.attributes or {}))
             for c in session.scalars(select(ChemComponent))]
    if not comps:
        use_default_chemistry()
        return
    reactions = [Reaction(r.name, dict(r.stoichiometry), r.k_ref, r.activation_energy,
                          dict(r.orders) if r.orders else None, r.heat_of_reaction, r.description)
                 for r in session.scalars(select(ChemReaction))]
    set_chemistry(comps, reactions)
