from mls.spec.model import Spec, is_terse, load_spec, spec_json_schema, spec_to_json
from mls.spec.check import check_spec
from mls.spec.syntax import locate


def load_checked(path):
    """`load_spec` then `check_spec`; the errors of a terse input carry their source span (#15)."""
    from mls.errors import SpecError

    spec = load_spec(path)
    try:
        return check_spec(spec)
    except SpecError as e:
        raise SpecError(locate(path, e.errors)) from None


__all__ = ["Spec", "is_terse", "load_spec", "load_checked", "locate", "spec_json_schema", "spec_to_json",
           "check_spec"]
