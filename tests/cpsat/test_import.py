from ortools.sat.python import cp_model


def test_can_import_cp_model_and_build_trivial_model() -> None:
    model = cp_model.CpModel()
    x = model.NewIntVar(0, 10, "x")
    assert x is not None
