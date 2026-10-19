import random

# =====================================================================
# Batches and lot quality.
#
# A raw-material delivery (a "lot") is described by its composition. A good lot is the nominal composition;
# a bad one has a lower assay (less of the active components, more solvent) and/or impurities that react
# in unwanted ways (see the A_I_to_X reaction). The result is a mole-fraction dict that a Source can supply
# (Source.set_lot) or that can be charged into a vessel as a batch (the `charge` scenario action).
# =====================================================================


def make_lot(base, assay=1.0, impurities=None, solvent="water"):
    """Return a mole-fraction composition for a delivery.

    base: nominal mole fractions, e.g. {"water": 0.8, "A": 0.2}.
    assay: 1.0 = nominal strength; 0.8 = every non-solvent component is 20% weaker (solvent fills the gap).
    impurities: extra mole fractions added on top, e.g. {"I": 0.02}. Everything is normalised at the end.
    """
    if assay < 0:
        raise ValueError("assay cannot be negative")
    lot = {c: (x if c == solvent else x * assay) for c, x in base.items()}
    for c, x in (impurities or {}).items():
        lot[c] = lot.get(c, 0.0) + x
    total = sum(lot.values())
    if total <= 0:
        raise ValueError("a lot needs some material")
    return {c: x / total for c, x in lot.items() if x > 0}


def random_lot(base, rng=None, assay_spread=0.05, impurity=None, impurity_max=0.02, solvent="water"):
    """A plausible random delivery: assay around 1 with a spread, plus a random amount of one impurity."""
    rng = rng or random.Random()
    assay = max(0.0, rng.gauss(1.0, assay_spread))
    imp = {impurity: rng.uniform(0.0, impurity_max)} if impurity else None
    return make_lot(base, assay, imp, solvent)
